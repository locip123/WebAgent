from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import stat
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from browser_use.webretriever.browser import (
	BrowserObservation,
	BrowserRuntime,
	ElementRef,
	_ElementBinding,
	cdp_headers_for_url,
	is_forbidden_search_url,
	is_sec_url,
	redact_cdp_url,
)
from browser_use.webretriever.models import AgentDecision

_VALID_PNG = base64.b64decode(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


class FakeLocator:
	def __init__(self, text: str = '', visible: bool = True, href: str = '') -> None:
		self.text = text
		self.visible = visible
		self.href = href
		self.calls: list[tuple[str, Any]] = []
		self.first = self

	async def click(self, **kwargs: Any) -> None:
		self.calls.append(('click', kwargs))

	async def dblclick(self, **kwargs: Any) -> None:
		self.calls.append(('dblclick', kwargs))

	async def fill(self, text: str, **kwargs: Any) -> None:
		self.calls.append(('fill', text))

	async def press(self, key: str, **kwargs: Any) -> None:
		self.calls.append(('press', key))

	async def type(self, text: str, **kwargs: Any) -> None:
		self.calls.append(('type', text))

	async def select_option(self, **kwargs: Any) -> list[str]:
		self.calls.append(('select_option', kwargs))
		return [str(next(iter(kwargs.values())))]

	async def hover(self, **kwargs: Any) -> None:
		self.calls.append(('hover', kwargs))

	async def evaluate(self, expression: str, value: Any = None) -> Any:
		self.calls.append(('evaluate', value))
		if 'interactiveIndex' in expression:
			revealed = bool(value and value.get('reveal'))
			return {
				'tag': 'a',
				'id': '',
				'href': self.href,
				'interactiveIndex': '',
				'inViewport': revealed,
				'revealed': revealed,
			}
		return {
			'tag': 'nav',
			'id': 'sidebar',
			'className': 'sidebar-inner',
			'beforeX': 0,
			'beforeY': 100,
			'afterX': 0,
			'afterY': 820,
		}

	async def drag_to(self, target: FakeLocator, **kwargs: Any) -> None:
		self.calls.append(('drag_to', target))

	async def inner_text(self, **kwargs: Any) -> str:
		return self.text

	async def count(self) -> int:
		return 1 if self.visible else 0

	def nth(self, index: int) -> FakeLocator:
		return self

	async def is_visible(self) -> bool:
		return self.visible


class FakeFrame:
	def __init__(self, url: str, *, body_text: str = '', element_text: str | None = None) -> None:
		self.url = url
		self.body_text = body_text
		self.element_text = element_text
		self.target_locator = FakeLocator()

	async def evaluate(self, expression: str, value: dict[str, Any]) -> Any:
		if 'const candidates' not in expression or self.element_text is None:
			return None
		index = value['start']
		return [
			{
				'index': index,
				'tag': 'button',
				'text': self.element_text,
				'name': self.element_text,
				'x': 10,
				'y': 20,
				'width': 80,
				'height': 30,
			}
		]

	def locator(self, selector: str) -> FakeLocator:
		if selector == 'body':
			return FakeLocator(self.body_text)
		return self.target_locator

	def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
		return FakeLocator(self.body_text, visible=text.casefold() in self.body_text.casefold())


class FakeMouse:
	def __init__(self) -> None:
		self.calls: list[tuple[Any, ...]] = []

	async def click(self, x: float, y: float, **kwargs: Any) -> None:
		self.calls.append(('click', x, y, kwargs))

	async def move(self, x: float, y: float, **kwargs: Any) -> None:
		self.calls.append(('move', x, y, kwargs))

	async def down(self) -> None:
		self.calls.append(('down',))

	async def up(self) -> None:
		self.calls.append(('up',))

	async def wheel(self, x: float, y: float) -> None:
		self.calls.append(('wheel', x, y))


class FakeKeyboard:
	def __init__(self) -> None:
		self.calls: list[str] = []

	async def press(self, key: str) -> None:
		self.calls.append(key)


class FakePage:
	def __init__(self, url: str = 'about:blank', frames: list[FakeFrame] | None = None) -> None:
		self.url = url
		self.frames = frames or [FakeFrame(url)]
		self.main_frame = self.frames[0]
		self.viewport_size = {'width': 1280, 'height': 720}
		self.mouse = FakeMouse()
		self.keyboard = FakeKeyboard()
		self.handlers: dict[str, list[Any]] = {}
		self.goto_urls: list[str] = []
		self.extra_http_headers: list[dict[str, str]] = []
		self.screenshot_paths: list[str] = []
		self.closed = False
		self.back_url = 'https://start.example/path'

	def set_default_timeout(self, timeout: int) -> None:
		self.default_timeout = timeout

	def set_default_navigation_timeout(self, timeout: int) -> None:
		self.default_navigation_timeout = timeout

	def on(self, event: str, handler: Any) -> None:
		self.handlers.setdefault(event, []).append(handler)

	def remove_listener(self, event: str, handler: Any) -> None:
		self.handlers.get(event, []).remove(handler)

	def is_closed(self) -> bool:
		return self.closed

	async def goto(self, url: str, **kwargs: Any) -> None:
		self.goto_urls.append(url)
		self.url = url
		self.main_frame.url = url

	async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
		self.extra_http_headers.append(headers)

	async def go_back(self, **kwargs: Any) -> None:
		self.url = self.back_url
		self.main_frame.url = self.back_url

	async def close(self, **kwargs: Any) -> None:
		self.closed = True

	async def wait_for_timeout(self, milliseconds: float) -> None:
		return None

	async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
		self.screenshot_paths.append(path)
		payload = f'image-{len(self.screenshot_paths)}'.encode()
		Path(path).write_bytes(payload)
		return payload

	async def title(self) -> str:
		return 'Fake title'

	async def evaluate(self, expression: str) -> dict[str, int]:
		return {'width': 1280, 'height': 720}

	def locator(self, selector: str) -> FakeLocator:
		return self.main_frame.locator(selector)

	async def bring_to_front(self) -> None:
		return None


class FakeContext:
	def __init__(self, pages: list[FakePage]) -> None:
		self.pages_to_create = pages
		self.handlers: dict[str, list[Any]] = {}
		self.removed: list[tuple[str, Any]] = []

	def on(self, event: str, handler: Any) -> None:
		self.handlers.setdefault(event, []).append(handler)

	def remove_listener(self, event: str, handler: Any) -> None:
		self.removed.append((event, handler))
		self.handlers.get(event, []).remove(handler)

	async def new_page(self) -> FakePage:
		return self.pages_to_create.pop(0)


def make_started_runtime(
	tmp_path: Path,
	page: FakePage,
	*,
	context: FakeContext | None = None,
	**runtime_kwargs: Any,
) -> BrowserRuntime:
	runtime = BrowserRuntime(
		context or FakeContext([]),  # type: ignore[arg-type]
		tmp_path,
		logging.getLogger('test-webretriever'),
		**runtime_kwargs,
	)
	runtime._started = True
	runtime.website = page.url
	runtime.page = page  # type: ignore[assignment]
	runtime._owned_pages = [page]  # type: ignore[list-item]
	runtime._last_safe_urls[id(page)] = page.url
	return runtime


def test_cdp_header_and_redaction_preserve_other_url_parts() -> None:
	url = 'https://sandbox.example/cdp?x=1&access_token=sit%2Fvery-secret&mode=ws#frag'
	assert cdp_headers_for_url(url) == {'X-Access-Token': 'sit/very-secret'}
	redacted = redact_cdp_url(url)
	assert 'very-secret' not in redacted
	assert redacted == 'https://sandbox.example/cdp?x=1&access_token=<redacted>&mode=ws#frag'
	assert cdp_headers_for_url('http://localhost:9222') == {}


@pytest.mark.parametrize(
	'url',
	[
		'https://www.google.com/search?q=secret',
		'https://www.bing.com/search?q=secret',
		'https://duckduckgo.com/?q=secret',
		'https://search.yahoo.com/search?p=secret',
		'https://www.baidu.com/s?wd=secret',
		'https://search.brave.com/search?q=secret',
	],
)
def test_known_external_search_urls_are_forbidden(url: str) -> None:
	assert is_forbidden_search_url(url)


def test_search_detection_does_not_use_unsafe_substrings() -> None:
	assert not is_forbidden_search_url('https://docs.google.com/document/d/example')
	assert not is_forbidden_search_url('https://example.test/?next=https://google.com/search')
	assert not is_forbidden_search_url('https://notgoogle.com/search?q=x')


@pytest.mark.parametrize('url', ['https://sec.gov', 'https://www.sec.gov/', 'https://data.sec.gov/submissions/CIK.json'])
def test_sec_detection_matches_only_sec_hosts(url: str) -> None:
	assert is_sec_url(url)


@pytest.mark.parametrize('url', ['https://notsec.gov', 'https://sec.gov.example.test', 'https://example.test/?next=sec.gov'])
def test_sec_detection_rejects_lookalike_hosts(url: str) -> None:
	assert not is_sec_url(url)


async def test_start_uses_exact_url_and_close_cleans_listeners_and_page(tmp_path: Path) -> None:
	page = FakePage()
	context = FakeContext([page])
	runtime = BrowserRuntime(context, tmp_path, logging.getLogger('test-webretriever'))  # type: ignore[arg-type]
	exact = 'https://example.test/a%2Fb?z=2&x=1#section'

	await runtime.start(exact)

	assert page.goto_urls == [exact]
	assert runtime.visited_urls == [exact]
	assert set(context.handlers) == {'page', 'request', 'response', 'requestfailed'}
	await runtime.close()
	assert page.closed
	assert len(context.removed) == 4
	assert all(not handlers for handlers in context.handlers.values())


async def test_sec_start_declares_user_agent_before_first_navigation(tmp_path: Path) -> None:
	page = FakePage()
	context = FakeContext([page])
	declared = 'Example Organization sec-admin@example.org'
	runtime = BrowserRuntime(
		context,
		tmp_path,
		logging.getLogger('test-webretriever'),
		declared_user_agent=declared,
	)  # type: ignore[arg-type]

	await runtime.start('https://www.sec.gov/Archives/edgar/data/1/')

	assert page.extra_http_headers == [{'User-Agent': declared}]
	assert page.goto_urls == ['https://www.sec.gov/Archives/edgar/data/1/']


async def test_declared_sec_user_agent_is_sent_on_first_navigation(monkeypatch, tmp_path: Path) -> None:
	seen_headers: dict[str, str] = {}

	async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
		raw_request = await reader.readuntil(b'\r\n\r\n')
		for line in raw_request.decode('latin-1').split('\r\n')[1:]:
			if line.lower().startswith('user-agent:'):
				seen_headers['user-agent'] = line.split(':', 1)[1].strip()
		writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
		await writer.drain()
		writer.close()
		await writer.wait_closed()

	server = await asyncio.start_server(handle_connection, '127.0.0.1', 0)
	port = server.sockets[0].getsockname()[1]
	declared = 'Example Organization sec-admin@example.org'
	monkeypatch.setattr('browser_use.webretriever.browser.is_sec_url', lambda _url: True)
	try:
		async with async_playwright() as playwright:
			browser = await playwright.chromium.launch(headless=True)
			context = await browser.new_context()
			runtime = BrowserRuntime(
				context,
				tmp_path,
				logging.getLogger('test-webretriever'),
				declared_user_agent=declared,
			)
			try:
				await runtime.start(f'http://127.0.0.1:{port}/')
			finally:
				await runtime.close()
				await browser.close()
	finally:
		server.close()
		await server.wait_closed()

	assert seen_headers['user-agent'] == declared


async def test_non_sec_start_does_not_declare_sec_user_agent(tmp_path: Path) -> None:
	page = FakePage()
	context = FakeContext([page])
	runtime = BrowserRuntime(
		context,
		tmp_path,
		logging.getLogger('test-webretriever'),
		declared_user_agent='Example Organization sec-admin@example.org',
	)  # type: ignore[arg-type]

	await runtime.start('https://example.test/')

	assert page.extra_http_headers == []


async def test_sec_user_agent_does_not_contaminate_a_later_non_sec_task(tmp_path: Path) -> None:
	sec_page = FakePage()
	non_sec_page = FakePage()
	context = FakeContext([sec_page, non_sec_page])
	declared = 'Example Organization sec-admin@example.org'
	sec_runtime = BrowserRuntime(
		context,
		tmp_path / 'sec',
		logging.getLogger('test-webretriever'),
		declared_user_agent=declared,
	)  # type: ignore[arg-type]
	await sec_runtime.start('https://www.sec.gov/')
	await sec_runtime.close()

	non_sec_runtime = BrowserRuntime(context, tmp_path / 'non-sec', logging.getLogger('test-webretriever'))  # type: ignore[arg-type]
	await non_sec_runtime.start('https://example.test/')

	assert sec_page.extra_http_headers == [{'User-Agent': declared}]
	assert non_sec_page.extra_http_headers == []


async def test_sec_new_tab_declares_user_agent(tmp_path: Path) -> None:
	first_page = FakePage()
	second_page = FakePage()
	context = FakeContext([first_page, second_page])
	declared = 'Example Organization sec-admin@example.org'
	runtime = BrowserRuntime(
		context,
		tmp_path,
		logging.getLogger('test-webretriever'),
		declared_user_agent=declared,
	)  # type: ignore[arg-type]
	await runtime.start('https://www.sec.gov/')

	await runtime.execute({'action': 'new_tab', 'url': 'https://www.sec.gov/Archives/edgar/data/1/'})

	assert first_page.extra_http_headers == [{'User-Agent': declared}]
	assert second_page.extra_http_headers == [{'User-Agent': declared}]
	assert second_page.goto_urls == ['https://www.sec.gov/Archives/edgar/data/1/']


async def test_sec_popup_declares_user_agent_for_follow_up_requests(tmp_path: Path) -> None:
	first_page = FakePage()
	popup_page = FakePage()
	context = FakeContext([first_page])
	declared = 'Example Organization sec-admin@example.org'
	runtime = BrowserRuntime(
		context,
		tmp_path,
		logging.getLogger('test-webretriever'),
		declared_user_agent=declared,
	)  # type: ignore[arg-type]
	await runtime.start('https://www.sec.gov/')

	runtime._on_context_page(popup_page)  # type: ignore[arg-type]
	await runtime._drain_background_tasks()

	assert popup_page.extra_http_headers == [{'User-Agent': declared}]


async def test_observe_enumerates_cross_frame_elements_and_saves_both_screenshots(tmp_path: Path) -> None:
	frames = [
		FakeFrame('https://example.test/', body_text='main body', element_text='Main button'),
		FakeFrame('https://frame.example/', body_text='frame body', element_text='Frame button'),
	]
	page = FakePage('https://example.test/', frames)
	runtime = make_started_runtime(tmp_path, page)

	observation = await runtime.observe(3)

	assert observation.screenshot == b'image-2'
	assert [element.index for element in observation.elements] == [0, 1]
	assert observation.elements[1].frame_index == 1
	assert '[Frame 1: https://frame.example/]' in observation.page_text
	assert (tmp_path / 'trajectory' / '3.png').read_bytes() == b'image-1'
	assert (tmp_path / 'trajectory_visual' / '3.png').read_bytes() == b'image-2'


async def test_observe_recovers_raw_screenshot_timeout_with_current_cdp_png(tmp_path: Path) -> None:
	class RawScreenshotTimeoutPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			self.screenshot_paths.append(path)
			if len(self.screenshot_paths) == 1:
				raise PlaywrightTimeoutError('raw screenshot timed out')
			Path(path).write_bytes(b'annotated-image')
			return b'annotated-image'

	class FakeCDPSession:
		detached = False

		async def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
			assert method == 'Page.captureScreenshot'
			return {'data': base64.b64encode(_VALID_PNG).decode('ascii')}

		async def detach(self) -> None:
			self.detached = True

	class ScreenshotContext(FakeContext):
		def __init__(self) -> None:
			super().__init__([])
			self.session = FakeCDPSession()

		async def new_cdp_session(self, page: FakePage) -> FakeCDPSession:
			return self.session

	page = RawScreenshotTimeoutPage('https://example.test/')
	context = ScreenshotContext()
	runtime = make_started_runtime(
		tmp_path,
		page,
		context=context,
		action_timeout_ms=30_000,
		screenshot_timeout_ms=25,
		cdp_screenshot_timeout_ms=25,
	)

	observation = await runtime.observe(0)

	assert observation.screenshot == b'annotated-image'
	assert (tmp_path / 'trajectory' / '0.png').read_bytes() == _VALID_PNG
	assert context.session.detached


async def test_observe_reports_both_failures_when_raw_cdp_recovery_fails(tmp_path: Path) -> None:
	class RawScreenshotTimeoutPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			raise PlaywrightTimeoutError('font readiness exhausted the screenshot deadline')

	class FailingCDPSession:
		detached = False

		async def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
			raise RuntimeError('renderer did not answer CDP capture')

		async def detach(self) -> None:
			self.detached = True

	class ScreenshotContext(FakeContext):
		def __init__(self) -> None:
			super().__init__([])
			self.session = FailingCDPSession()

		async def new_cdp_session(self, page: FakePage) -> FailingCDPSession:
			return self.session

	page = RawScreenshotTimeoutPage('https://example.test/')
	context = ScreenshotContext()
	runtime = make_started_runtime(
		tmp_path,
		page,
		context=context,
		screenshot_timeout_ms=25,
		cdp_screenshot_timeout_ms=25,
	)

	with pytest.raises(RuntimeError) as error:
		await runtime.observe(0)

	message = str(error.value)
	assert 'font readiness exhausted the screenshot deadline' in message
	assert 'renderer did not answer CDP capture' in message
	assert context.session.detached


async def test_observe_does_not_use_cdp_for_raw_non_timeout_error(tmp_path: Path) -> None:
	class RawScreenshotErrorPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			raise RuntimeError('raw renderer failed without a timeout')

	class NoFallbackContext(FakeContext):
		async def new_cdp_session(self, page: FakePage) -> None:
			raise AssertionError('non-timeout screenshot errors must not use CDP')

	page = RawScreenshotErrorPage('https://example.test/')
	context = NoFallbackContext([])
	runtime = make_started_runtime(tmp_path, page, context=context)

	with pytest.raises(RuntimeError, match='raw renderer failed without a timeout'):
		await runtime.observe(0)


async def test_observe_does_not_wait_past_cdp_deadline_when_session_release_hangs(tmp_path: Path) -> None:
	class RawScreenshotTimeoutPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			raise PlaywrightTimeoutError('raw screenshot deadline expired')

	release_detach = asyncio.Event()

	class HangingCDPSession:
		async def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
			await asyncio.Event().wait()
			raise AssertionError('unreachable')

		async def detach(self) -> None:
			await release_detach.wait()

	class ScreenshotContext(FakeContext):
		async def new_cdp_session(self, page: FakePage) -> HangingCDPSession:
			return HangingCDPSession()

	page = RawScreenshotTimeoutPage('https://example.test/')
	context = ScreenshotContext([])
	runtime = make_started_runtime(
		tmp_path,
		page,
		context=context,
		screenshot_timeout_ms=25,
		cdp_screenshot_timeout_ms=25,
	)

	observation_task = asyncio.create_task(runtime.observe(0))
	done, _ = await asyncio.wait({observation_task}, timeout=0.2)
	try:
		assert observation_task in done, 'CDP recovery exceeded its hard deadline while releasing the session'
		with pytest.raises(RuntimeError, match='CDP fallback failed'):
			await observation_task
	finally:
		release_detach.set()
		if not observation_task.done():
			await asyncio.gather(observation_task, return_exceptions=True)


async def test_timed_out_cdp_recovery_cannot_write_a_late_screenshot(tmp_path: Path) -> None:
	class RawScreenshotTimeoutPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			raise PlaywrightTimeoutError('raw screenshot deadline expired')

	release_capture = asyncio.Event()
	capture_finished = asyncio.Event()

	class CancellationResistantCDPSession:
		async def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
			try:
				await release_capture.wait()
			except asyncio.CancelledError:
				await release_capture.wait()
			return {'data': base64.b64encode(_VALID_PNG).decode('ascii')}

		async def detach(self) -> None:
			capture_finished.set()

	class ScreenshotContext(FakeContext):
		async def new_cdp_session(self, page: FakePage) -> CancellationResistantCDPSession:
			return CancellationResistantCDPSession()

	page = RawScreenshotTimeoutPage('https://example.test/')
	context = ScreenshotContext([])
	runtime = make_started_runtime(
		tmp_path,
		page,
		context=context,
		screenshot_timeout_ms=25,
		cdp_screenshot_timeout_ms=25,
	)

	with pytest.raises(RuntimeError, match='CDP fallback failed'):
		await runtime.observe(0)

	raw_path = tmp_path / 'trajectory' / '0.png'
	assert not raw_path.exists()
	release_capture.set()
	await asyncio.wait_for(capture_finished.wait(), timeout=0.2)
	assert not raw_path.exists()


async def test_observe_recovers_both_screenshots_when_page_font_never_finishes(tmp_path: Path) -> None:
	html = (
		b'<!doctype html><title>Pending font page</title><style>'
		b"@font-face{font-family:Stall;src:url('/stall.woff2')}body{font-family:Stall,sans-serif}"
		b'</style><button>ready</button>'
	)
	release_font = asyncio.Event()
	handler_tasks: set[asyncio.Task[Any]] = set()

	async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
		task = asyncio.current_task()
		if task is not None:
			handler_tasks.add(task)
		try:
			request = await reader.readuntil(b'\r\n\r\n')
			path = request.split(b' ', 2)[1]
			if path == b'/stall.woff2':
				writer.write(
					b'HTTP/1.1 200 OK\r\n'
					b'Content-Type: font/woff2\r\n'
					b'Content-Length: 999999\r\n'
					b'Connection: close\r\n\r\n'
				)
				await writer.drain()
				await release_font.wait()
			else:
				writer.write(
					b'HTTP/1.1 200 OK\r\n'
					b'Content-Type: text/html\r\n'
					+ f'Content-Length: {len(html)}\r\n'.encode()
					+ b'Connection: close\r\n\r\n'
					+ html
				)
				await writer.drain()
		except (asyncio.IncompleteReadError, ConnectionError):
			pass
		finally:
			if task is not None:
				handler_tasks.discard(task)
			writer.close()
			with contextlib.suppress(Exception):
				await writer.wait_closed()

	server = await asyncio.start_server(handle_connection, '127.0.0.1', 0)
	port = server.sockets[0].getsockname()[1]
	start_url = f'http://127.0.0.1:{port}/'
	runtime: BrowserRuntime | None = None
	try:
		async with async_playwright() as playwright:
			browser = await playwright.chromium.launch(headless=True)
			context = await browser.new_context()
			runtime = BrowserRuntime(
				context,
				tmp_path,
				logging.getLogger('test-webretriever'),
				screenshot_timeout_ms=250,
				cdp_screenshot_timeout_ms=1_000,
			)
			try:
				await runtime.start(start_url)
				assert await runtime.page.evaluate('document.fonts.status') == 'loading'
				observation = await runtime.observe(0)
			finally:
				release_font.set()
				await runtime.close()
				await browser.close()
	finally:
		release_font.set()
		server.close()
		await server.wait_closed()
		if handler_tasks:
			await asyncio.gather(*tuple(handler_tasks), return_exceptions=True)

	raw = (tmp_path / 'trajectory' / '0.png').read_bytes()
	visual = (tmp_path / 'trajectory_visual' / '0.png').read_bytes()
	assert raw.startswith(_VALID_PNG[:8])
	assert visual.startswith(_VALID_PNG[:8])
	assert observation.screenshot == visual
	assert observation.url == start_url
	assert observation.title == 'Pending font page'
	assert (observation.viewport_width, observation.viewport_height) == (1280, 720)
	assert any(element.text == 'ready' for element in observation.elements)


async def test_observe_keeps_raw_screenshot_when_annotated_capture_has_non_timeout_error(tmp_path: Path) -> None:
	class AnnotatedScreenshotErrorPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			self.screenshot_paths.append(path)
			if len(self.screenshot_paths) == 2:
				raise RuntimeError('annotated renderer failed')
			Path(path).write_bytes(_VALID_PNG)
			return _VALID_PNG

	class NoFallbackContext(FakeContext):
		async def new_cdp_session(self, page: FakePage) -> None:
			raise AssertionError('non-timeout screenshot errors must not use CDP')

	page = AnnotatedScreenshotErrorPage('https://example.test/')
	context = NoFallbackContext([])
	runtime = make_started_runtime(tmp_path, page, context=context)

	observation = await runtime.observe(0)

	assert observation.screenshot == _VALID_PNG
	assert (tmp_path / 'trajectory' / '0.png').read_bytes() == _VALID_PNG
	assert not (tmp_path / 'trajectory_visual' / '0.png').exists()


async def test_observe_propagates_annotated_cdp_recovery_failure(tmp_path: Path) -> None:
	class AnnotatedScreenshotTimeoutPage(FakePage):
		async def screenshot(self, *, path: str, **kwargs: Any) -> bytes:
			self.screenshot_paths.append(path)
			if len(self.screenshot_paths) == 2:
				raise PlaywrightTimeoutError('annotated screenshot deadline expired')
			Path(path).write_bytes(_VALID_PNG)
			return _VALID_PNG

	class FailingCDPSession:
		async def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
			raise RuntimeError('annotated CDP capture failed')

		async def detach(self) -> None:
			return None

	class ScreenshotContext(FakeContext):
		async def new_cdp_session(self, page: FakePage) -> FailingCDPSession:
			return FailingCDPSession()

	page = AnnotatedScreenshotTimeoutPage('https://example.test/')
	context = ScreenshotContext([])
	runtime = make_started_runtime(
		tmp_path,
		page,
		context=context,
		screenshot_timeout_ms=25,
		cdp_screenshot_timeout_ms=25,
	)

	with pytest.raises(RuntimeError) as error:
		await runtime.observe(0)

	assert 'annotated screenshot deadline expired' in str(error.value)
	assert 'annotated CDP capture failed' in str(error.value)


async def test_model_actions_double_click_hover_xy_drag_and_page_scroll(tmp_path: Path) -> None:
	page = FakePage('https://example.test/')
	runtime = make_started_runtime(tmp_path, page)
	locator = page.main_frame.target_locator
	runtime._element_bindings[4] = _ElementBinding(page.main_frame, '[data-webretriever-index="4"]')  # type: ignore[arg-type]

	await runtime.execute(AgentDecision(action='double_click', element_id=4))
	await runtime.execute(AgentDecision(action='hover_xy', x=10, y=20))
	await runtime.execute(AgentDecision(action='drag', x=1, y=2, end_x=101, end_y=202))
	await runtime.execute(AgentDecision(action='scroll', direction='down', pages=2))

	assert locator.calls[0][0] == 'dblclick'
	assert ('move', 10.0, 20.0, {}) in page.mouse.calls
	assert ('move', 1.0, 2.0, {}) in page.mouse.calls
	assert ('move', 101.0, 202.0, {'steps': 12}) in page.mouse.calls
	assert ('wheel', 0.0, 1440.0) in page.mouse.calls


async def test_scroll_pages_can_target_an_observed_element(tmp_path: Path) -> None:
	page = FakePage('https://example.test/')
	runtime = make_started_runtime(tmp_path, page)
	locator = page.main_frame.target_locator
	runtime._element_bindings[2] = _ElementBinding(page.main_frame, '[data-webretriever-index="2"]')  # type: ignore[arg-type]

	result = await runtime.execute(AgentDecision(action='scroll', element_id=2, direction='up', pages=1))

	assert locator.calls == [('evaluate', {'x': 0.0, 'y': -720.0})]
	assert result == 'scrolled nav#sidebar from (0, 100) to (0, 820); requested (0, -720)'


async def test_direct_search_navigation_is_rejected_and_click_escape_is_rolled_back(tmp_path: Path) -> None:
	page = FakePage('https://start.example/path')
	runtime = make_started_runtime(tmp_path, page)
	with pytest.raises(ValueError, match='search engines'):
		await runtime.execute(AgentDecision(action='navigate', url='https://www.google.com/search?q=forbidden'))

	class EscapingLocator(FakeLocator):
		async def click(self, **kwargs: Any) -> None:
			page.url = 'https://www.bing.com/search?q=escaped'
			page.main_frame.url = page.url

	page.main_frame.target_locator = EscapingLocator()
	runtime._element_bindings[1] = _ElementBinding(page.main_frame, '[data-webretriever-index="1"]')  # type: ignore[arg-type]
	await runtime.execute(AgentDecision(action='click', element_id=1))
	assert page.url == 'https://start.example/path'
	assert all(not is_forbidden_search_url(url) for url in runtime.visited_urls)


async def test_find_text_searches_full_download_text(tmp_path: Path) -> None:
	page = FakePage('https://example.test/')
	runtime = make_started_runtime(tmp_path, page)
	runtime.downloads.append({'filename': 'report.pdf', 'text': ('before ' * 2_000) + 'needle answer 42' + (' after' * 2_000)})

	result = await runtime.execute(AgentDecision(action='find_text', text='needle'))

	assert 'download report.pdf:' in result
	assert 'needle answer 42' in result


async def test_find_text_reopens_truncated_csv_to_search_late_rows(tmp_path: Path) -> None:
	page = FakePage('https://example.test/')
	runtime = make_started_runtime(tmp_path, page)
	path = runtime.download_dir / 'large.csv'
	path.write_text(('prefix,0\n' * 40_000) + 'Qatar,QAT,2000,592468\n', encoding='utf-8')
	runtime.downloads.append(
		{
			'filename': path.name,
			'path': str(path),
			'text': path.read_text(encoding='utf-8')[:250_000],
			'text_truncated': True,
		}
	)

	result = await runtime.execute(AgentDecision(action='find_text', text='Qatar,QAT,2000'))

	assert 'download large.csv:' in result
	assert 'Qatar,QAT,2000,592468' in result


async def test_find_text_returns_authoritative_link_href(tmp_path: Path) -> None:
	href = 'https://statistics.example/graph/#exact-target'
	frame = FakeFrame('https://statistics.example/graph/', body_text='Foreigners Entries by Port of Entry and Month')
	frame.get_by_text = lambda text, exact=False: FakeLocator(text, href=href)  # type: ignore[method-assign]
	page = FakePage('https://statistics.example/graph/', [frame])
	runtime = make_started_runtime(tmp_path, page)

	result = await runtime.execute(AgentDecision(action='find_text', text='Foreigners Entries by Port of Entry and Month'))

	assert f'href={href}' in result
	assert 'in_viewport=true' in result
	assert 'revealed_for_next_observation=true' in result


async def test_calculate_finds_fastest_growth_deterministically(tmp_path: Path) -> None:
	page = FakePage('https://example.test/')
	runtime = make_started_runtime(tmp_path, page)
	decision = AgentDecision(
		action='calculate',
		operation='argmax_growth',
		text='{"2008":11.026,"2005":6.465,"2007":9.141,"2006":8.5}',
	)

	result = json.loads(await runtime.execute(decision))

	assert result['point_count'] == 4
	assert result['winner']['label'] == '2006'
	assert result['winner']['previous_label'] == '2005'
	assert result['winner']['metric'] == pytest.approx((8.5 - 6.465) / 6.465)


def test_observation_render_keeps_network_and_download_evidence_after_long_page_text() -> None:
	observation = BrowserObservation(
		screenshot=b'',
		url='https://example.test/',
		title='title',
		tabs=[],
		viewport_width=1280,
		viewport_height=720,
		elements=[ElementRef(index=0, tag='button', text='go')],
		page_text='p' * 200_000,
		recent_network=[{'method': 'GET', 'url': 'https://example.test/api/important', 'status': 200}],
		downloads=[{'filename': 'important.pdf', 'text': 'download evidence'}],
	)

	rendered = observation.render_text()
	assert 'api/important' in rendered
	assert 'download evidence' in rendered
	assert len(rendered) <= 100_000


def test_observation_render_is_bounded_with_pathological_tabs_and_elements() -> None:
	long_value = 'x' * 20_000
	observation = BrowserObservation(
		screenshot=b'',
		url='https://example.test/' + long_value,
		title=long_value,
		tabs=[{'index': index, 'title': long_value, 'url': f'https://tab-{index}.test/{long_value}'} for index in range(200)],
		viewport_width=1280,
		viewport_height=720,
		elements=[ElementRef(index=index, tag='a', text=long_value, href=long_value) for index in range(2_000)],
		page_text=long_value * 20,
		recent_network=[{'method': 'GET', 'url': 'https://network-evidence.test/answer?' + long_value, 'status': 200}],
		downloads=[{'filename': 'download-evidence.pdf', 'text': 'document answer 42 ' + long_value}],
	)

	rendered = observation.render_text()
	assert len(rendered) <= 100_000
	assert 'network-evidence.test/answer' in rendered
	assert 'download-evidence.pdf' in rendered
	assert 'document answer 42' in rendered


class FakeRequest:
	resource_type = 'xhr'
	url = 'https://example.test/api/data'
	method = 'POST'
	headers = {'content-type': 'application/json'}
	post_data = '{"query":"widgets"}'
	post_data_buffer = post_data.encode()
	failure = None


class FakeResponse:
	def __init__(self, request: FakeRequest) -> None:
		self.request = request
		self.status = 200
		self.headers = {'content-type': 'application/json', 'content-length': '11'}

	async def body(self) -> bytes:
		return b'{"ok":true}'


async def test_capture_schema_is_official_compatible_and_has_bounded_response_body(tmp_path: Path) -> None:
	runtime = BrowserRuntime(FakeContext([]), tmp_path, logging.getLogger('test-webretriever'), max_response_body_bytes=64)  # type: ignore[arg-type]
	request = FakeRequest()
	runtime._on_request(request)  # type: ignore[arg-type]
	await runtime._capture_response(FakeResponse(request), runtime.all_requests[0])  # type: ignore[arg-type]
	payload = runtime.capture_payload()

	assert set(payload) == {'capture_time', 'total_requests', 'all_requests'}
	assert payload['total_requests'] == 1
	entry = payload['all_requests'][0]
	assert entry['post_data'] == '{"query":"widgets"}'
	assert entry['json_data'] == {'query': 'widgets'}
	assert entry['response_json'] == {'ok': True}
	assert entry['response_body_bytes'] == 11


async def test_capture_redacts_declared_sec_user_agent(tmp_path: Path) -> None:
	declared = 'Example Organization sec-admin@example.org'
	runtime = BrowserRuntime(
		FakeContext([]),
		tmp_path,
		logging.getLogger('test-webretriever'),
		declared_user_agent=declared,
	)  # type: ignore[arg-type]
	request = FakeRequest()
	request.headers = {'content-type': 'application/json', 'user-agent': declared}

	runtime._on_request(request)  # type: ignore[arg-type]

	assert runtime.capture_payload()['all_requests'][0]['headers']['user-agent'] == '<redacted>'


async def test_inline_pdf_document_response_is_saved_extracted_and_deduplicated(tmp_path: Path) -> None:
	from reportlab.pdfgen.canvas import Canvas

	stream = BytesIO()
	canvas = Canvas(stream)
	canvas.drawString(72, 720, 'Protocol PDF answer 42')
	canvas.save()
	pdf = stream.getvalue()

	class DocumentRequest:
		resource_type = 'document'

	class DocumentResponse:
		request = DocumentRequest()
		url = 'https://example.test/reports/final'
		status = 200
		headers = {
			'content-type': 'application/pdf',
			'content-disposition': 'inline; filename="result.pdf"',
			'content-length': str(len(pdf)),
		}

		async def body(self) -> bytes:
			return pdf

	runtime = BrowserRuntime(FakeContext([]), tmp_path, logging.getLogger('test-webretriever'))  # type: ignore[arg-type]
	response = DocumentResponse()
	runtime._on_response(response)  # type: ignore[arg-type]
	runtime._on_response(response)  # type: ignore[arg-type]  # duplicate response must not duplicate the extracted file
	await runtime._drain_download_tasks()

	assert len(runtime.downloads) == 1
	item = runtime.downloads[0]
	assert item['source'] == 'document_response'
	assert item['filename'] == 'result.pdf'
	assert 'Protocol PDF answer 42' in item['text']
	assert Path(item['path']).read_bytes() == pdf


async def _download_observation(
	httpserver: Any,
	tmp_path: Path,
	*,
	route: str,
	filename: str,
	payload: bytes,
	find_query: str | None = None,
) -> tuple[BrowserObservation, str]:
	httpserver.expect_request(route).respond_with_data(
		payload,
		content_type='application/zip',
		headers={'Content-Disposition': f'attachment; filename="{filename}"'},
	)
	async with async_playwright() as playwright:
		browser = await playwright.chromium.launch(headless=True)
		context = await browser.new_context(accept_downloads=True)
		runtime = BrowserRuntime(context, tmp_path, logging.getLogger('test-webretriever'))
		try:
			await runtime.start(httpserver.url_for(route))
			observation = await runtime.observe(0)
			search_result = await runtime.execute({'action': 'find', 'query': find_query}) if find_query else ''
		finally:
			await runtime.close()
			await browser.close()
	return observation, search_result


async def test_downloaded_zip_exposes_searchable_archive_members(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr('reports/Data.csv', 'country,2005,2006\nChina,6.465,8.5\n')
	observation, search_result = await _download_observation(
		httpserver,
		tmp_path,
		route='/official-data.ZIP',
		filename='official-data.ZIP',
		payload=stream.getvalue(),
		find_query='China,6.465',
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'official-data.ZIP')
	member_item = next(item for item in observation.downloads if item['filename'] == 'reports/Data.csv')
	assert (tmp_path / 'downloads' / 'official-data.ZIP').is_file()
	assert (tmp_path / 'downloads' / 'reports' / 'Data.csv').read_text() == 'country,2005,2006\nChina,6.465,8.5\n'
	assert archive_item['extraction_status'] == 'success'
	assert archive_item['extracted_count'] == 1
	assert 'China,6.465' not in archive_item['text']
	assert member_item['source'] == 'archive_member'
	assert member_item['source_archive'] == 'official-data.ZIP'
	assert 'download reports/Data.csv:' in search_result


async def test_downloaded_zip_preserves_safe_members_without_overwriting_files(httpserver, tmp_path: Path) -> None:
	download_dir = tmp_path / 'downloads'
	(download_dir / 'shared').mkdir(parents=True)
	(download_dir / 'shared' / 'Data.csv').write_text('existing evidence\n', encoding='utf-8')
	(download_dir / 'blocked').write_text('directory name is already a file\n', encoding='utf-8')
	(download_dir / 'broken.csv').symlink_to(download_dir / 'missing-target.csv')

	nested_stream = BytesIO()
	with zipfile.ZipFile(nested_stream, 'w') as nested_archive:
		nested_archive.writestr('secret/inner.csv', 'must not be recursively extracted\n')
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr('shared/Data.csv', 'new evidence\n')
		archive.writestr('blocked/report.csv', 'directory conflict resolved\n')
		archive.writestr('broken.csv', 'broken symlink conflict resolved\n')
		archive.writestr('nested.zip', nested_stream.getvalue())
		archive.writestr('../escape.txt', 'unsafe\n')
		archive.writestr('/absolute.txt', 'unsafe\n')
		archive.writestr('C:/drive.txt', 'unsafe\n')
		archive.writestr('D:drive-relative.txt', 'unsafe\n')
		symlink = zipfile.ZipInfo('link.txt')
		symlink.create_system = 3
		symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
		archive.writestr(symlink, 'shared/Data.csv')
		fifo = zipfile.ZipInfo('pipe')
		fifo.create_system = 3
		fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
		archive.writestr(fifo, b'')
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/mixed.zip',
		filename='mixed.zip',
		payload=stream.getvalue(),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'mixed.zip')
	member_names = {item['filename'] for item in observation.downloads if item.get('source') == 'archive_member'}
	assert (download_dir / 'shared' / 'Data.csv').read_text() == 'existing evidence\n'
	assert (download_dir / 'shared' / 'Data_1.csv').read_text() == 'new evidence\n'
	assert (download_dir / 'blocked').read_text() == 'directory name is already a file\n'
	assert (download_dir / 'blocked_1' / 'report.csv').read_text() == 'directory conflict resolved\n'
	assert (download_dir / 'broken.csv').is_symlink()
	assert (download_dir / 'broken_1.csv').read_text() == 'broken symlink conflict resolved\n'
	assert (download_dir / 'nested.zip').is_file()
	assert not (download_dir / 'secret').exists()
	assert not (tmp_path / 'escape.txt').exists()
	assert not (download_dir / 'absolute.txt').exists()
	assert not (download_dir / 'C:').exists()
	assert not (download_dir / 'link.txt').exists()
	assert not (download_dir / 'pipe').exists()
	assert member_names == {'shared/Data_1.csv', 'blocked_1/report.csv', 'broken_1.csv', 'nested.zip'}
	assert archive_item['extraction_status'] == 'partial'
	assert archive_item['extracted_count'] == 4
	assert archive_item['skipped_count'] == 6
	assert len(archive_item['extraction_warnings']) == 6


async def test_downloaded_zip_skips_member_larger_than_100_mib(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	megabyte = b'\0' * (1024 * 1024)
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		with archive.open('too-large.bin', 'w') as member:
			for _ in range(101):
				member.write(megabyte)
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/large-member.zip',
		filename='large-member.zip',
		payload=stream.getvalue(),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'large-member.zip')
	assert (tmp_path / 'downloads' / 'large-member.zip').is_file()
	assert not (tmp_path / 'downloads' / 'too-large.bin').exists()
	assert 'failure' not in archive_item
	assert archive_item['extraction_status'] == 'failed'
	assert archive_item['extracted_count'] == 0
	assert archive_item['skipped_count'] == 1
	assert any('100 MiB' in warning for warning in archive_item['extraction_warnings'])
	assert not list((tmp_path / 'downloads').glob('.*.part'))


async def test_downloaded_zip_stops_before_250_mib_total_output(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	megabyte = b'\0' * (1024 * 1024)
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		for name, size_mib in (
			('too-large.bin', 101),
			('retained.bin', 90),
			('budget-exhausted.bin', 90),
		):
			with archive.open(name, 'w') as member:
				for _ in range(size_mib):
					member.write(megabyte)
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/large-total.zip',
		filename='large-total.zip',
		payload=stream.getvalue(),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'large-total.zip')
	assert not (tmp_path / 'downloads' / 'too-large.bin').exists()
	assert (tmp_path / 'downloads' / 'retained.bin').stat().st_size == 90 * 1024 * 1024
	assert not (tmp_path / 'downloads' / 'budget-exhausted.bin').exists()
	assert archive_item['extraction_status'] == 'partial'
	assert archive_item['extracted_count'] == 1
	assert archive_item['skipped_count'] == 2
	assert any('100 MiB' in warning for warning in archive_item['extraction_warnings'])
	assert any('250 MiB' in warning for warning in archive_item['extraction_warnings'])
	assert not list((tmp_path / 'downloads').glob('.*.part'))


async def test_downloaded_zip_uses_written_bytes_instead_of_declared_size(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr('declared-huge.txt', 'small actual output\n')
	payload = bytearray(stream.getvalue())
	with zipfile.ZipFile(BytesIO(payload)) as archive:
		info = archive.getinfo('declared-huge.txt')
	declared_size = (2**32 - 2).to_bytes(4, 'little')
	payload[info.header_offset + 22 : info.header_offset + 26] = declared_size
	central_offset = payload.find(b'PK\x01\x02')
	assert central_offset >= 0
	payload[central_offset + 24 : central_offset + 28] = declared_size

	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/misreported-size.zip',
		filename='misreported-size.zip',
		payload=bytes(payload),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'misreported-size.zip')
	member_item = next(item for item in observation.downloads if item.get('source') == 'archive_member')
	assert (tmp_path / 'downloads' / 'declared-huge.txt').read_text() == 'small actual output\n'
	assert member_item['size_bytes'] == len(b'small actual output\n')
	assert archive_item['extraction_status'] == 'success'


async def test_downloaded_zip_exposes_at_most_1000_members(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		for index in range(1001):
			archive.writestr(f'rows/file-{index:04}.txt', '')
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/many-members.zip',
		filename='many-members.zip',
		payload=stream.getvalue(),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'many-members.zip')
	member_items = [item for item in observation.downloads if item.get('source') == 'archive_member']
	assert (tmp_path / 'downloads' / 'rows' / 'file-0999.txt').is_file()
	assert not (tmp_path / 'downloads' / 'rows' / 'file-1000.txt').exists()
	assert len(member_items) == 1000
	assert archive_item['extraction_status'] == 'partial'
	assert archive_item['extracted_count'] == 1000
	assert archive_item['skipped_count'] == 1
	assert any('1,000' in warning for warning in archive_item['extraction_warnings'])
	assert 'ZIP extraction partial: 1000 file(s) extracted, 1 member(s) skipped.' in observation.render_text()


async def test_downloaded_zip_bounds_member_warnings_without_hiding_skip_count(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
		for index in range(150):
			archive.writestr(f'../escape-{index}.txt', 'unsafe\n')
		archive.writestr('safe.txt', 'usable evidence\n')
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/many-warnings.zip',
		filename='many-warnings.zip',
		payload=stream.getvalue(),
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'many-warnings.zip')
	assert (tmp_path / 'downloads' / 'safe.txt').read_text() == 'usable evidence\n'
	assert archive_item['extraction_status'] == 'partial'
	assert archive_item['extracted_count'] == 1
	assert archive_item['skipped_count'] == 150
	assert len(archive_item['extraction_warnings']) == 100
	assert archive_item['extraction_warnings'][-1] == '51 additional warning(s) omitted'
	assert '51 additional warning(s) omitted' in archive_item['text']


async def test_downloaded_zip_isolates_crc_and_encrypted_member_failures(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
		archive.writestr('before.txt', 'before remains usable\n')
		archive.writestr('bad-crc.txt', 'corrupt this member\n')
		archive.writestr('encrypted.txt', 'password required\n')
		archive.writestr(f'{"x" * 300}/too-long.txt', 'filesystem path failure\n')
		archive.writestr('after.txt', 'after remains usable\n')
	payload = bytearray(stream.getvalue())
	with zipfile.ZipFile(BytesIO(payload)) as archive:
		bad_crc = archive.getinfo('bad-crc.txt')
		name_length = int.from_bytes(payload[bad_crc.header_offset + 26 : bad_crc.header_offset + 28], 'little')
		extra_length = int.from_bytes(payload[bad_crc.header_offset + 28 : bad_crc.header_offset + 30], 'little')
		data_offset = bad_crc.header_offset + 30 + name_length + extra_length
		payload[data_offset] ^= 0xFF

	central_offset = payload.find(b'PK\x01\x02')
	while central_offset >= 0 and payload[central_offset : central_offset + 4] == b'PK\x01\x02':
		name_length = int.from_bytes(payload[central_offset + 28 : central_offset + 30], 'little')
		extra_length = int.from_bytes(payload[central_offset + 30 : central_offset + 32], 'little')
		comment_length = int.from_bytes(payload[central_offset + 32 : central_offset + 34], 'little')
		name_start = central_offset + 46
		name = bytes(payload[name_start : name_start + name_length]).decode()
		if name == 'encrypted.txt':
			flags = int.from_bytes(payload[central_offset + 8 : central_offset + 10], 'little') | 1
			payload[central_offset + 8 : central_offset + 10] = flags.to_bytes(2, 'little')
			local_offset = int.from_bytes(payload[central_offset + 42 : central_offset + 46], 'little')
			local_flags = int.from_bytes(payload[local_offset + 6 : local_offset + 8], 'little') | 1
			payload[local_offset + 6 : local_offset + 8] = local_flags.to_bytes(2, 'little')
			break
		central_offset = name_start + name_length + extra_length + comment_length

	observation, search_result = await _download_observation(
		httpserver,
		tmp_path,
		route='/member-failures.zip',
		filename='member-failures.zip',
		payload=bytes(payload),
		find_query='after remains usable',
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'member-failures.zip')
	member_names = {item['filename'] for item in observation.downloads if item.get('source') == 'archive_member'}
	assert (tmp_path / 'downloads' / 'before.txt').read_text() == 'before remains usable\n'
	assert (tmp_path / 'downloads' / 'after.txt').read_text() == 'after remains usable\n'
	assert not (tmp_path / 'downloads' / 'bad-crc.txt').exists()
	assert not (tmp_path / 'downloads' / 'encrypted.txt').exists()
	assert member_names == {'before.txt', 'after.txt'}
	assert archive_item['extraction_status'] == 'partial'
	assert archive_item['extracted_count'] == 2
	assert archive_item['skipped_count'] == 3
	assert any('bad-crc.txt' in warning for warning in archive_item['extraction_warnings'])
	assert any('encrypted.txt' in warning for warning in archive_item['extraction_warnings'])
	assert any('too-long.txt' in warning for warning in archive_item['extraction_warnings'])
	assert 'download after.txt:' in search_result
	assert not list((tmp_path / 'downloads').glob('.*.part'))


async def test_invalid_downloaded_zip_keeps_download_success_separate_from_extraction(httpserver, tmp_path: Path) -> None:
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/invalid.zip',
		filename='invalid.zip',
		payload=b'not a ZIP archive',
	)

	archive_item = next(item for item in observation.downloads if item['filename'] == 'invalid.zip')
	assert (tmp_path / 'downloads' / 'invalid.zip').read_bytes() == b'not a ZIP archive'
	assert 'failure' not in archive_item
	assert archive_item['extraction_status'] == 'failed'
	assert archive_item['extracted_count'] == 0
	assert archive_item['skipped_count'] == 0
	assert archive_item['extraction_warnings'] == ['Downloaded file has a .zip suffix but is not a valid ZIP archive']
	assert not [item for item in observation.downloads if item.get('source') == 'archive_member']


async def test_valid_zip_without_zip_filename_is_not_automatically_extracted(httpserver, tmp_path: Path) -> None:
	stream = BytesIO()
	with zipfile.ZipFile(stream, 'w') as archive:
		archive.writestr('hidden.txt', 'must remain inside the unrecognised archive\n')
	observation, _ = await _download_observation(
		httpserver,
		tmp_path,
		route='/export.bin',
		filename='export.bin',
		payload=stream.getvalue(),
	)

	download_item = next(item for item in observation.downloads if item['filename'] == 'export.bin')
	assert (tmp_path / 'downloads' / 'export.bin').is_file()
	assert not (tmp_path / 'downloads' / 'hidden.txt').exists()
	assert 'extraction_status' not in download_item
	assert not [item for item in observation.downloads if item.get('source') == 'archive_member']


def test_document_response_uses_observed_spreadsheet_extension() -> None:
	class DocumentRequest:
		resource_type = 'document'

	class SpreadsheetResponse:
		request = DocumentRequest()
		url = 'https://example.test/report'
		headers = {
			'content-type': 'application/vnd.ms-excel',
			'content-disposition': 'inline; filename="bid-summary.xlsx"',
		}

	assert BrowserRuntime._document_response_extension(SpreadsheetResponse()) == '.xlsx'  # type: ignore[arg-type]


def test_text_and_xlsx_download_extraction(tmp_path: Path) -> None:
	text_path = tmp_path / 'answer.csv'
	text_path.write_text('name,value\nanswer,42\n', encoding='utf-8')
	text, truncated = BrowserRuntime._extract_download_text(text_path)
	assert 'answer,42' in text
	assert not truncated
	json_path = tmp_path / 'answer.json'
	json_path.write_text(json.dumps({'answer': 42}), encoding='utf-8')
	text, truncated = BrowserRuntime._extract_download_text(json_path)
	assert '"answer": 42' in text
	assert not truncated

	from docx import Document

	docx_path = tmp_path / 'answer.docx'
	document = Document()
	document.add_paragraph('DOCX answer 42')
	document.save(str(docx_path))
	text, truncated = BrowserRuntime._extract_download_text(docx_path)
	assert 'DOCX answer 42' in text
	assert not truncated

	xlsx_path = tmp_path / 'answer.xlsx'
	shared_xml = (
		'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
		'<si><t>answer</t></si><si><t>forty two</t></si></sst>'
	)
	sheet_xml = (
		'<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
		'<sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row></sheetData></worksheet>'
	)
	with zipfile.ZipFile(xlsx_path, 'w') as archive:
		archive.writestr('xl/sharedStrings.xml', shared_xml)
		archive.writestr('xl/worksheets/sheet1.xml', sheet_xml)
	text, truncated = BrowserRuntime._extract_download_text(xlsx_path)
	assert 'answer\tforty two' in text
	assert not truncated

	zip_path = tmp_path / 'official-data.zip'
	with zipfile.ZipFile(zip_path, 'w') as archive:
		archive.writestr('Metadata.csv', 'indicator,unit\nrate,per 1000\n')
		archive.writestr('Data.csv', 'country,2005,2006\nChina,6.465,8.5\n')
		archive.writestr('ignored.bin', b'\x00\x01')
	text, truncated = BrowserRuntime._extract_download_text(zip_path)
	assert '[Data.csv]' in text
	assert 'China,6.465,8.5' in text
	assert 'ignored.bin' not in text
	assert not truncated
