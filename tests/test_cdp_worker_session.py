from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from playwright._impl._errors import TargetClosedError

from browser_use.webretriever.browser_session import (
	BrowserRecoveryDeadlineExceeded,
	CdpWorkerSession,
	TaskBrowserRequest,
)
from browser_use.webretriever.connection import BrowserConnector, BrowserDriver


async def _serve_page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
	await reader.read(65_536)
	body = b'<html><body>session-alive</body></html>'
	writer.write(
		b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: '
		+ str(len(body)).encode()
		+ b'\r\nConnection: close\r\n\r\n'
		+ body
	)
	await writer.drain()
	writer.close()
	await writer.wait_closed()


async def _wait_for_devtools_port(profile_dir: Path) -> str:
	active_port = profile_dir / 'DevToolsActivePort'
	for _ in range(100):
		if active_port.exists():
			return f'http://127.0.0.1:{active_port.read_text(encoding="utf-8").splitlines()[0]}'
		await asyncio.sleep(0.05)
	raise AssertionError('Chromium did not publish its DevTools port')


async def _close_all_initial_pages(endpoint: str) -> None:
	async with async_playwright() as playwright:
		browser = await playwright.chromium.connect_over_cdp(endpoint)
		for context in browser.contexts:
			for page in list(context.pages):
				await page.close(run_before_unload=False)
		await browser.close()


async def _cdp_targets(endpoint: str) -> list[dict[str, object]]:
	def load() -> list[dict[str, object]]:
		with urllib.request.urlopen(f'{endpoint}/json/list', timeout=2) as response:
			return json.load(response)

	return await asyncio.to_thread(load)


async def _task_runtime(
	session: CdpWorkerSession,
	*,
	website: str,
	task_dir: Path,
):
	return await session.open_task_runtime(
		TaskBrowserRequest(website=website, task_dir=task_dir, logger=logging.getLogger('cdp-session-test')),
		deadline_monotonic=time.monotonic() + 10,
	)


class _ManualClock:
	def __init__(self) -> None:
		self.now = 100.0

	def monotonic(self) -> float:
		return self.now

	async def sleep(self, seconds: float) -> None:
		self.now += seconds


class _DelayedConnector:
	def __init__(self, *, clock: _ManualClock, available_at: float) -> None:
		self.clock = clock
		self.available_at = available_at
		self.real = BrowserConnector()

	async def connect(self, *args, **kwargs):
		if self.clock.monotonic() < self.available_at:
			raise OSError('sandbox endpoint is temporarily unavailable')
		return await self.real.connect(*args, **kwargs)


class _UnavailableConnector:
	async def connect(self, *args, **kwargs):
		raise OSError('sandbox endpoint remains unavailable')


class _AnchorPage:
	def is_closed(self) -> bool:
		return False


class _ContextThatClosesBeforeTheTaskPage:
	def __init__(self) -> None:
		self.pages = []
		self._anchor = _AnchorPage()

	def on(self, _event, _handler) -> None:
		return None

	def remove_listener(self, _event, _handler) -> None:
		return None

	async def new_page(self):
		if not self.pages:
			self.pages.append(self._anchor)
			return self._anchor
		raise TargetClosedError('BrowserContext.new_page: Target page, context or browser has been closed')


class _ClosedBrowser:
	def __init__(self) -> None:
		self.contexts = [_ContextThatClosesBeforeTheTaskPage()]


class _ClosedConnection:
	def __init__(self) -> None:
		self.browser = _ClosedBrowser()
		self.driver = BrowserDriver.PLAYWRIGHT
		self.fallback_reason = None
		self.rebrowser_runtime_fix_mode = None

	async def close(self) -> None:
		return None


class _ClosedThenRealConnector:
	def __init__(self) -> None:
		self._closed_connection_available = True
		self.real = BrowserConnector()

	async def connect(self, *args, **kwargs):
		if self._closed_connection_available:
			self._closed_connection_available = False
			return _ClosedConnection()
		return await self.real.connect(*args, **kwargs)


class _HangingContext:
	pages = [_AnchorPage()]

	def on(self, _event, _handler) -> None:
		return None

	def remove_listener(self, _event, _handler) -> None:
		return None

	async def new_page(self):
		await asyncio.Future()


class _HangingConnection:
	def __init__(self) -> None:
		self.browser = type('HangingBrowser', (), {'contexts': [_HangingContext()]})()

	async def close(self) -> None:
		return None


def test_cdp_worker_session_keeps_the_sandbox_alive_between_tasks(tmp_path: Path) -> None:
	async def scenario() -> None:
		server = await asyncio.start_server(_serve_page, '127.0.0.1', 0)
		website = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
		with tempfile.TemporaryDirectory(prefix='wr-cdp-session-') as profile:
			profile_dir = Path(profile)
			async with async_playwright() as playwright:
				process = await asyncio.create_subprocess_exec(
					playwright.chromium.executable_path,
					'--headless=new',
					'--no-sandbox',
					'--disable-gpu',
					'--remote-debugging-port=0',
					f'--user-data-dir={profile_dir}',
					'about:blank',
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
				)
				try:
					endpoint = await _wait_for_devtools_port(profile_dir)
					await _close_all_initial_pages(endpoint)
					session = CdpWorkerSession(
						cdp_url=endpoint,
						driver=BrowserDriver.PLAYWRIGHT,
						headers=None,
						logger=logging.getLogger('cdp-session-test'),
						connector=BrowserConnector(),
					)
					try:
						first = await _task_runtime(session, website=website, task_dir=tmp_path / 'first')
						await first.close(timeout_seconds=5)
						second = await _task_runtime(session, website=website, task_dir=tmp_path / 'second')
						assert second.page is not None
						assert await second.page.text_content('body') == 'session-alive'
						await second.close(timeout_seconds=5)
					finally:
						await session.close()
				finally:
					if process.returncode is None:
						process.terminate()
						await process.wait()
		server.close()
		await server.wait_closed()

	asyncio.run(scenario())


def test_cdp_worker_session_recovers_before_the_task_deadline(tmp_path: Path) -> None:
	async def scenario() -> None:
		server = await asyncio.start_server(_serve_page, '127.0.0.1', 0)
		website = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
		with tempfile.TemporaryDirectory(prefix='wr-cdp-recovery-') as profile:
			profile_dir = Path(profile)
			async with async_playwright() as playwright:
				process = await asyncio.create_subprocess_exec(
					playwright.chromium.executable_path,
					'--headless=new',
					'--no-sandbox',
					'--disable-gpu',
					'--remote-debugging-port=0',
					f'--user-data-dir={profile_dir}',
					'about:blank',
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
				)
				try:
					endpoint = await _wait_for_devtools_port(profile_dir)
					clock = _ManualClock()
					session = CdpWorkerSession(
						cdp_url=endpoint,
						driver=BrowserDriver.PLAYWRIGHT,
						headers=None,
						logger=logging.getLogger('cdp-session-recovery-test'),
						connector=_DelayedConnector(clock=clock, available_at=103.0),
						monotonic=clock.monotonic,
						sleep=clock.sleep,
					)
					try:
						runtime = await session.open_task_runtime(
							TaskBrowserRequest(
								website=website,
								task_dir=tmp_path / 'recovered',
								logger=logging.getLogger('cdp-session-recovery-test'),
							),
							deadline_monotonic=110.0,
						)
						assert runtime.page is not None
						assert await runtime.page.text_content('body') == 'session-alive'
						await runtime.close(timeout_seconds=5)
					finally:
						await session.close()
				finally:
					if process.returncode is None:
						process.terminate()
						await process.wait()
		server.close()
		await server.wait_closed()

	asyncio.run(scenario())


def test_cdp_worker_session_stops_recovery_at_the_original_task_deadline(tmp_path: Path) -> None:
	async def scenario() -> None:
		clock = _ManualClock()
		session = CdpWorkerSession(
			cdp_url='https://sandbox.example/cdp?access_token=secret',
			driver=BrowserDriver.PLAYWRIGHT,
			headers={'X-Access-Token': 'secret'},
			logger=logging.getLogger('cdp-session-deadline-test'),
			connector=_UnavailableConnector(),
			monotonic=clock.monotonic,
			sleep=clock.sleep,
		)
		with pytest.raises(BrowserRecoveryDeadlineExceeded):
			await session.open_task_runtime(
				TaskBrowserRequest(
					website='https://example.test/',
					task_dir=tmp_path / 'deadline',
					logger=logging.getLogger('cdp-session-deadline-test'),
				),
				deadline_monotonic=103.0,
			)
		assert clock.monotonic() == 103.0

	asyncio.run(scenario())


def test_task_page_open_timeout_is_a_browser_recovery_deadline(tmp_path: Path) -> None:
	async def scenario() -> None:
		session = await CdpWorkerSession.from_connection(
			cdp_url='https://sandbox.example/cdp?access_token=secret',
			driver=BrowserDriver.PLAYWRIGHT,
			headers={'X-Access-Token': 'secret'},
			logger=logging.getLogger('cdp-session-page-timeout-test'),
			connector=_UnavailableConnector(),
			connection=_HangingConnection(),
		)
		with pytest.raises(BrowserRecoveryDeadlineExceeded):
			await session.open_task_runtime(
				TaskBrowserRequest(
					website='https://example.test/',
					task_dir=tmp_path / 'page-timeout',
					logger=logging.getLogger('cdp-session-page-timeout-test'),
				),
				deadline_monotonic=time.monotonic() + 0.02,
			)

	asyncio.run(scenario())


def test_cdp_worker_session_reconnects_when_the_task_page_finds_a_closed_context(tmp_path: Path) -> None:
	async def scenario() -> None:
		server = await asyncio.start_server(_serve_page, '127.0.0.1', 0)
		website = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
		with tempfile.TemporaryDirectory(prefix='wr-cdp-new-page-recovery-') as profile:
			profile_dir = Path(profile)
			async with async_playwright() as playwright:
				process = await asyncio.create_subprocess_exec(
					playwright.chromium.executable_path,
					'--headless=new',
					'--no-sandbox',
					'--disable-gpu',
					'--remote-debugging-port=0',
					f'--user-data-dir={profile_dir}',
					'about:blank',
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
				)
				try:
					endpoint = await _wait_for_devtools_port(profile_dir)
					clock = _ManualClock()
					session = CdpWorkerSession(
						cdp_url=endpoint,
						driver=BrowserDriver.PLAYWRIGHT,
						headers=None,
						logger=logging.getLogger('cdp-new-page-recovery-test'),
						connector=_ClosedThenRealConnector(),
						monotonic=clock.monotonic,
						sleep=clock.sleep,
					)
					try:
						runtime = await session.open_task_runtime(
							TaskBrowserRequest(
								website=website,
								task_dir=tmp_path / 'new-page-recovered',
								logger=logging.getLogger('cdp-new-page-recovery-test'),
							),
							deadline_monotonic=110.0,
						)
						assert runtime.page is not None
						assert await runtime.page.text_content('body') == 'session-alive'
						await runtime.close(timeout_seconds=5)
					finally:
						await session.close()
				finally:
					if process.returncode is None:
						process.terminate()
						await process.wait()
		server.close()
		await server.wait_closed()

	asyncio.run(scenario())


def test_task_cleanup_replaces_a_lost_anchor_before_closing_task_pages(tmp_path: Path) -> None:
	async def scenario() -> None:
		server = await asyncio.start_server(_serve_page, '127.0.0.1', 0)
		website = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
		with tempfile.TemporaryDirectory(prefix='wr-cdp-anchor-recovery-') as profile:
			profile_dir = Path(profile)
			async with async_playwright() as playwright:
				process = await asyncio.create_subprocess_exec(
					playwright.chromium.executable_path,
					'--headless=new',
					'--no-sandbox',
					'--disable-gpu',
					'--remote-debugging-port=0',
					f'--user-data-dir={profile_dir}',
					'about:blank',
					stdout=asyncio.subprocess.DEVNULL,
					stderr=asyncio.subprocess.DEVNULL,
				)
				try:
					endpoint = await _wait_for_devtools_port(profile_dir)
					await _close_all_initial_pages(endpoint)
					session = CdpWorkerSession(
						cdp_url=endpoint,
						driver=BrowserDriver.PLAYWRIGHT,
						headers=None,
						logger=logging.getLogger('cdp-anchor-recovery-test'),
						connector=BrowserConnector(connect_timeout_ms=200),
					)
					try:
						first = await _task_runtime(session, website=website, task_dir=tmp_path / 'anchor-first')
						admin = await playwright.chromium.connect_over_cdp(endpoint)
						for context in admin.contexts:
							for page in list(context.pages):
								if page.url == 'about:blank':
									await page.close(run_before_unload=False)
						await admin.close()
						await first.close(timeout_seconds=5)
						page_targets = [target for target in await _cdp_targets(endpoint) if target.get('type') == 'page']
						if not page_targets:
							process.terminate()
							await process.wait()
						second = await _task_runtime(session, website=website, task_dir=tmp_path / 'anchor-second')
						assert second.page is not None
						assert await second.page.text_content('body') == 'session-alive'
						await second.close(timeout_seconds=5)
					finally:
						await session.close()
				finally:
					if process.returncode is None:
						process.terminate()
						await process.wait()
		server.close()
		await server.wait_closed()

	asyncio.run(scenario())
