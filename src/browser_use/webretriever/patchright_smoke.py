"""Non-evaluative compatibility smoke test for Patchright CDP attachments.

The smoke test creates a fresh context on each supplied CDP browser and only
uses synthetic HTML plus a route-fulfilled request.  It does not navigate to a
challenge website, modify browser identity, or make an outbound network call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any, AsyncContextManager, Sequence, cast

__all__ = ['CdpSmokeResult', 'main', 'smoke_patchright_cdps']

_SMOKE_REQUEST_URL = 'https://webretriever-smoke.invalid/ping'


@dataclass(frozen=True, slots=True)
class CdpSmokeResult:
	"""Observable result of one non-destructive CDP compatibility check."""

	endpoint_label: str
	screenshot_bytes: int
	button_clicked: bool
	request_captured: bool
	response_captured: bool
	fetch_payload: dict[str, str]

	def to_dict(self) -> dict[str, object]:
		return asdict(self)


async def smoke_patchright_cdps(cdp_urls: Sequence[str]) -> list[CdpSmokeResult]:
	"""Exercise Patchright attach, isolated context, screenshot, click, and fetch.

	The connection is intentionally discarded with the Patchright client manager
	instead of calling ``Browser.close()``, so the evaluator-owned browser process
	remains running.
	"""
	if not cdp_urls:
		raise ValueError('provide at least one CDP URL')
	try:
		patchright = import_module('patchright.async_api')
	except ImportError as exc:
		raise RuntimeError('Patchright is not installed; install the pinned dependency first') from exc
	async_playwright = getattr(patchright, 'async_playwright', None)
	if not callable(async_playwright):
		raise RuntimeError('Patchright does not expose async_playwright')

	results: list[CdpSmokeResult] = []
	for index, cdp_url in enumerate(cdp_urls):
		client_manager = cast(AsyncContextManager[Any], async_playwright())
		async with client_manager as client:
			browser = await client.chromium.connect_over_cdp(cdp_url, timeout=60_000)
			context = await browser.new_context(viewport={'width': 960, 'height': 540})
			try:
				page = await context.new_page()
				request_captured = False
				response_captured = False

				def on_request(request: Any) -> None:
					nonlocal request_captured
					if request.url == _SMOKE_REQUEST_URL:
						request_captured = True

				def on_response(response: Any) -> None:
					nonlocal response_captured
					if response.url == _SMOKE_REQUEST_URL:
						response_captured = True

				async def fulfill_smoke_request(route: Any) -> None:
					await route.fulfill(
						status=200,
						content_type='application/json',
						headers={'Access-Control-Allow-Origin': 'null'},
						body='{"source":"patchright-smoke"}',
					)

				page.on('request', on_request)
				page.on('response', on_response)
				await page.route(f'{_SMOKE_REQUEST_URL}*', fulfill_smoke_request)
				await page.set_content(
					'<button id="verify">click</button><output id="out"></output>'
					'<script>document.querySelector("#verify").onclick=()=>document.querySelector("#out").textContent="clicked"</script>'
				)
				await page.locator('#verify').click()
				button_clicked = await page.locator('#out').text_content() == 'clicked'
				screenshot = await page.screenshot()
				fetch_payload = await page.evaluate(
					"""async (url) => {
						const response = await fetch(url);
						return await response.json();
					}""",
					_SMOKE_REQUEST_URL,
				)
				if not isinstance(fetch_payload, dict) or fetch_payload.get('source') != 'patchright-smoke':
					raise RuntimeError('route-fulfilled Patchright fetch returned an unexpected payload')
				if not (button_clicked and request_captured and response_captured and screenshot):
					raise RuntimeError('Patchright smoke operation was not observed end to end')
				results.append(
					CdpSmokeResult(
						endpoint_label=f'cdp-{index}',
						screenshot_bytes=len(screenshot),
						button_clicked=button_clicked,
						request_captured=request_captured,
						response_captured=response_captured,
						fetch_payload={'source': 'patchright-smoke'},
					)
				)
			finally:
				await context.close()
	return results


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Run the non-destructive Patchright CDP compatibility smoke test.')
	parser.add_argument('--cdp-url', action='append', dest='cdp_urls', required=True, help='CDP endpoint; repeat for each browser')
	return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
	"""Run the smoke test without model or competition-task credentials."""

	args = _parse_args(argv)
	try:
		results = asyncio.run(smoke_patchright_cdps(args.cdp_urls))
	except Exception as exc:
		print(f'Patchright CDP smoke test failed: {type(exc).__name__}: {exc}')
		return 1
	print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
