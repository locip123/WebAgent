"""CDP connection adapter shared by standard Playwright and Patchright tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from typing import Any

from playwright.async_api import async_playwright

__all__ = ['BrowserConnector', 'BrowserDriver', 'CdpConnection']


class BrowserDriver(str, Enum):
	"""The client used to attach to an already-running Chromium browser."""

	PLAYWRIGHT = 'playwright'
	PATCHRIGHT = 'patchright'


@dataclass(slots=True)
class CdpConnection:
	"""One owned Playwright-compatible client attached to one CDP endpoint."""

	browser: Any
	driver: BrowserDriver
	client_manager: Any
	fallback_reason: str | None = None

	async def close(self) -> None:
		"""Close the browser connection and then its client process."""

		try:
			await self.browser.close()
		finally:
			await self.client_manager.__aexit__(None, None, None)


class BrowserConnector:
	"""Attach one compatible browser client to a CDP endpoint.

	Patchright is optional and imported only when selected.  If it cannot attach
	before a worker starts its first task, the connector opens a standard
	Playwright client instead.  No mid-task driver switch is possible because the
	returned :class:`CdpConnection` owns one browser session.
	"""

	def __init__(
		self,
		*,
		playwright_factory: Callable[[], Any] = async_playwright,
		patchright_factory: Callable[[], Any] | None = None,
		connect_timeout_ms: float = 60_000,
	) -> None:
		self.playwright_factory = playwright_factory
		self.patchright_factory = patchright_factory
		self.connect_timeout_ms = connect_timeout_ms

	async def connect(
		self,
		driver: BrowserDriver,
		cdp_url: str,
		*,
		headers: Mapping[str, str] | None = None,
	) -> CdpConnection:
		"""Connect using ``driver`` and fall back only from Patchright startup."""

		try:
			return await self._connect(driver, cdp_url, headers=headers)
		except Exception as exc:
			if driver is not BrowserDriver.PATCHRIGHT:
				raise
			fallback_reason = self._safe_error(exc, cdp_url)
			return await self._connect(
				BrowserDriver.PLAYWRIGHT,
				cdp_url,
				headers=headers,
				fallback_reason=fallback_reason,
			)

	async def _connect(
		self,
		driver: BrowserDriver,
		cdp_url: str,
		*,
		headers: Mapping[str, str] | None,
		fallback_reason: str | None = None,
	) -> CdpConnection:
		manager = self._factory_for(driver)()
		client = await manager.__aenter__()
		try:
			browser = await client.chromium.connect_over_cdp(
				cdp_url,
				headers=dict(headers) if headers else None,
				timeout=self.connect_timeout_ms,
			)
		except BaseException as exc:
			await manager.__aexit__(type(exc), exc, exc.__traceback__)
			raise
		return CdpConnection(
			browser=browser,
			driver=driver,
			client_manager=manager,
			fallback_reason=fallback_reason,
		)

	def _factory_for(self, driver: BrowserDriver) -> Callable[[], Any]:
		if driver is BrowserDriver.PLAYWRIGHT:
			return self.playwright_factory
		if self.patchright_factory is not None:
			return self.patchright_factory
		try:
			patchright_module = import_module('patchright.async_api')
		except ImportError as exc:
			raise RuntimeError('Patchright is not installed; install the pinned experiment dependency first') from exc
		patchright_async_playwright = getattr(patchright_module, 'async_playwright', None)
		if not callable(patchright_async_playwright):
			raise RuntimeError('Patchright does not expose async_playwright')
		return patchright_async_playwright

	@staticmethod
	def _safe_error(exc: Exception, cdp_url: str) -> str:
		message = str(exc).replace(cdp_url, '<redacted>')
		return f'{type(exc).__name__}: {message}'[:2_000]
