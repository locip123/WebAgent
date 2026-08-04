from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from browser_use.webretriever.connection import BrowserConnector, BrowserDriver


@dataclass
class _FakeBrowser:
	closed: bool = False

	async def close(self) -> None:
		self.closed = True


@dataclass
class _FakeChromium:
	browser: _FakeBrowser = field(default_factory=_FakeBrowser)
	error: Exception | None = None
	calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

	async def connect_over_cdp(self, cdp_url: str, **kwargs: Any) -> _FakeBrowser:
		self.calls.append((cdp_url, kwargs))
		if self.error is not None:
			raise self.error
		return self.browser


@dataclass
class _FakeClient:
	chromium: _FakeChromium


@dataclass
class _FakeManager:
	client: _FakeClient
	exited: bool = False

	async def __aenter__(self) -> _FakeClient:
		return self.client

	async def __aexit__(self, *_args: object) -> None:
		self.exited = True


@pytest.mark.asyncio
async def test_standard_connector_connects_over_cdp_and_closes_its_client() -> None:
	manager = _FakeManager(_FakeClient(_FakeChromium()))
	connector = BrowserConnector(playwright_factory=lambda: manager)

	connection = await connector.connect(BrowserDriver.PLAYWRIGHT, 'http://127.0.0.1:9222', headers={'X-Test': 'yes'})

	assert connection.driver is BrowserDriver.PLAYWRIGHT
	assert connection.fallback_reason is None
	assert manager.client.chromium.calls == [
		('http://127.0.0.1:9222', {'headers': {'X-Test': 'yes'}, 'timeout': 60_000})
	]
	await connection.close()
	assert manager.client.chromium.browser.closed is True
	assert manager.exited is True


@pytest.mark.asyncio
async def test_patchright_connection_falls_back_before_any_task_starts() -> None:
	patchright_manager = _FakeManager(_FakeClient(_FakeChromium(error=RuntimeError('CDP mismatch'))))
	playwright_manager = _FakeManager(_FakeClient(_FakeChromium()))
	connector = BrowserConnector(
		playwright_factory=lambda: playwright_manager,
		patchright_factory=lambda: patchright_manager,
	)

	connection = await connector.connect(BrowserDriver.PATCHRIGHT, 'http://127.0.0.1:9223')

	assert connection.driver is BrowserDriver.PLAYWRIGHT
	assert connection.fallback_reason == 'RuntimeError: CDP mismatch'
	assert patchright_manager.exited is True
	assert playwright_manager.client.chromium.calls == [('http://127.0.0.1:9223', {'headers': None, 'timeout': 60_000})]
	await connection.close()
