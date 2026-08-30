"""Worker-scoped browser session ownership for formal CDP runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from browser_use.webretriever.browser import BrowserRuntime, is_browser_session_closed_error, redact_cdp_url
from browser_use.webretriever.connection import BrowserConnector, BrowserDriver, CdpConnection

__all__ = ['BrowserRecoveryDeadlineExceeded', 'CdpWorkerSession', 'TaskBrowserRequest']


class BrowserRecoveryDeadlineExceeded(TimeoutError):
	"""The worker could not restore its CDP session before the task deadline."""


@dataclass(frozen=True, slots=True)
class TaskBrowserRequest:
	"""Inputs required to open one task-owned browser runtime."""

	website: str
	task_dir: Path
	logger: logging.Logger
	declared_user_agent: str | None = None
	task_identity: Mapping[str, Any] | None = None


class CdpWorkerSession:
	"""Own one evaluator CDP connection and keep its sandbox alive between tasks."""

	def __init__(
		self,
		*,
		cdp_url: str,
		driver: BrowserDriver,
		headers: Mapping[str, str] | None,
		logger: logging.Logger,
		connector: BrowserConnector,
		monotonic: Callable[[], float] = time.monotonic,
		sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
	) -> None:
		self.cdp_url = cdp_url
		self.driver = driver
		self.headers = dict(headers) if headers else None
		self.logger = logger
		self.connector = connector
		self._monotonic = monotonic
		self._sleep = sleep
		self.connection: CdpConnection | None = None
		self.context: BrowserContext | None = None
		self._anchor_page: Page | None = None
		self._recovery_required = False

	@classmethod
	async def from_connection(
		cls,
		*,
		cdp_url: str,
		driver: BrowserDriver,
		headers: Mapping[str, str] | None,
		logger: logging.Logger,
		connector: BrowserConnector,
		connection: CdpConnection,
	) -> CdpWorkerSession:
		"""Take ownership of the worker's initial, untimed evaluator connection."""

		self = cls(cdp_url=cdp_url, driver=driver, headers=headers, logger=logger, connector=connector)
		browser = connection.browser
		context = browser.contexts[0] if browser.contexts else await browser.new_context(accept_downloads=True)
		anchor_page = context.pages[0] if context.pages else await context.new_page()
		self.connection = connection
		self.context = context
		self._anchor_page = anchor_page
		return self

	@property
	def recovery_required(self) -> bool:
		"""Whether a failed task requires worker-session recovery before new work."""

		return self._recovery_required

	async def abandon_interrupted_task(self) -> None:
		"""Discard the old client after a task lost all of its owned pages."""

		await self._invalidate_connection()
		self._recovery_required = True

	async def recover_before_next_task(self, *, deadline_monotonic: float) -> None:
		"""Rebuild a clean CDP context and anchor before claiming another task."""

		if not self._recovery_required:
			return
		await self._connect_until(deadline_monotonic, fresh_context=True)
		self._recovery_required = False

	async def open_task_runtime(
		self,
		request: TaskBrowserRequest,
		*,
		deadline_monotonic: float,
	) -> BrowserRuntime:
		"""Open the task start page while retaining a worker-owned anchor page."""

		if self._recovery_required:
			await self.recover_before_next_task(deadline_monotonic=deadline_monotonic)
		while True:
			remaining = deadline_monotonic - self._monotonic()
			if remaining <= 0:
				raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline')
			if self.connection is None:
				await self._connect_until(deadline_monotonic)
			if self.context is None:
				raise RuntimeError('CDP worker session has no browser context')
			if self._anchor_page is None or self._anchor_page.is_closed():
				try:
					self._anchor_page = await asyncio.wait_for(
						self.context.new_page(),
						timeout=max(0.0, deadline_monotonic - self._monotonic()),
					)
				except TimeoutError as exc:
					await self._invalidate_connection()
					raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline') from exc
				except PlaywrightError as exc:
					if not is_browser_session_closed_error(exc):
						raise
					await self._invalidate_connection()
					continue

			runtime = BrowserRuntime(
				self.context,
				request.task_dir,
				request.logger,
				declared_user_agent=request.declared_user_agent,
				task_identity=request.task_identity,
				before_close=self._restore_anchor_before_cleanup,
			)
			try:
				await asyncio.wait_for(
					runtime.start(request.website),
					timeout=max(0.0, deadline_monotonic - self._monotonic()),
				)
			except TimeoutError as exc:
				with contextlib.suppress(Exception):
					await runtime.close(timeout_seconds=1.0)
				await self._invalidate_connection()
				raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline') from exc
			except PlaywrightError as exc:
				if not is_browser_session_closed_error(exc):
					raise
				remaining = deadline_monotonic - self._monotonic()
				if remaining > 0:
					with contextlib.suppress(Exception):
						await runtime.close(timeout_seconds=min(5.0, remaining))
				await self._invalidate_connection()
				remaining = deadline_monotonic - self._monotonic()
				if remaining <= 0:
					raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline') from exc
				self.logger.warning('CDP context closed before task browsing; reconnecting within the task deadline')
				await self._sleep(min(1.0, remaining))
				continue
			return runtime

	async def _connect_until(self, deadline_monotonic: float, *, fresh_context: bool = False) -> None:
		retry_delay = 1.0
		last_error: BaseException | None = None
		while True:
			remaining = deadline_monotonic - self._monotonic()
			if remaining <= 0:
				raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline') from last_error
			connection: CdpConnection | None = None
			try:
				connection = await asyncio.wait_for(
					self.connector.connect(self.driver, self.cdp_url, headers=self.headers),
					timeout=remaining,
				)
				browser = connection.browser
				if browser.contexts and not fresh_context:
					context = browser.contexts[0]
				else:
					context = await asyncio.wait_for(
						browser.new_context(accept_downloads=True),
						timeout=max(0.0, deadline_monotonic - self._monotonic()),
					)
				anchor_page = (
					context.pages[0]
					if context.pages
					else await asyncio.wait_for(
						context.new_page(),
						timeout=max(0.0, deadline_monotonic - self._monotonic()),
					)
				)
			except (OSError, PlaywrightError, TimeoutError) as exc:
				last_error = exc
				if connection is not None:
					try:
						await connection.close()
					except Exception:
						pass
				remaining = deadline_monotonic - self._monotonic()
				if remaining <= 0:
					raise BrowserRecoveryDeadlineExceeded('Browser recovery exhausted the task deadline') from exc
				self.logger.warning(
					'CDP connection unavailable; retrying within the current task deadline: %s',
					redact_cdp_url(f'{type(exc).__name__}: {exc}'),
				)
				await self._sleep(min(retry_delay, remaining))
				retry_delay = min(retry_delay * 2, 30.0)
				continue
			self.connection = connection
			self.context = context
			self._anchor_page = anchor_page
			return

	async def _invalidate_connection(self) -> None:
		connection, self.connection = self.connection, None
		self.context = None
		self._anchor_page = None
		if connection is not None:
			with contextlib.suppress(Exception):
				await connection.close()

	async def _restore_anchor_before_cleanup(self) -> None:
		if self.context is None:
			return
		if self._anchor_page is not None and not self._anchor_page.is_closed():
			return
		self._anchor_page = await self.context.new_page()

	async def close(self) -> None:
		"""Disconnect this worker's Playwright client from the evaluator browser."""

		await self._invalidate_connection()
		self._recovery_required = False
