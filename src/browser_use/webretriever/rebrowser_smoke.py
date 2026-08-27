"""Offline compatibility smoke test for the Rebrowser CDP driver."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Sequence
from urllib.parse import quote

from browser_use.webretriever.connection import BrowserConnector, BrowserDriver

__all__ = ['RebrowserSmokeResult', 'main', 'smoke_rebrowser_cdp']


@dataclass(frozen=True, slots=True)
class RebrowserSmokeResult:
	"""Evidence that the patched client retained required browser primitives."""

	endpoint_label: str
	runtime_fix_mode: str
	button_clicked: bool
	evaluation_result: str
	screenshot_bytes: int

	def to_dict(self) -> dict[str, object]:
		return asdict(self)


async def smoke_rebrowser_cdp(cdp_url: str) -> RebrowserSmokeResult:
	"""Attach to one CDP browser and exercise only a local ``data:`` page.

	``page.set_content`` is deliberately not used: it relies on Playwright's
	console-event path, which the Rebrowser Runtime.Enable mitigation disables.
	The production agent navigates to task URLs and does not use that API.
	"""

	connection = await BrowserConnector().connect(BrowserDriver.REBROWSER, cdp_url)
	context = None
	try:
		context = await connection.browser.new_context(viewport={'width': 960, 'height': 540})
		page = await context.new_page()
		html = (
			'<title>Rebrowser smoke</title><button id="verify">click</button><output id="out"></output>'
			'<script>verify.onclick=()=>out.textContent="clicked"</script>'
		)
		await page.goto(f'data:text/html,{quote(html)}', wait_until='domcontentloaded')
		await page.locator('#verify').click()
		button_clicked = await page.locator('#out').text_content() == 'clicked'
		evaluation_result = await page.evaluate('() => document.querySelector("#out").textContent')
		screenshot = await page.screenshot()
		if not (button_clicked and evaluation_result == 'clicked' and screenshot):
			raise RuntimeError('Rebrowser smoke operation was not observed end to end')
		return RebrowserSmokeResult(
			endpoint_label='cdp-0',
			runtime_fix_mode=connection.rebrowser_runtime_fix_mode or 'unknown',
			button_clicked=button_clicked,
			evaluation_result=evaluation_result,
			screenshot_bytes=len(screenshot),
		)
	finally:
		if context is not None:
			await context.close()
		await connection.close()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Run the offline Rebrowser CDP compatibility smoke test.')
	parser.add_argument('--cdp-url', required=True, help='one evaluator CDP endpoint')
	return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
	args = _parse_args(argv)
	try:
		result = asyncio.run(smoke_rebrowser_cdp(args.cdp_url))
	except Exception as exc:
		print(f'Rebrowser CDP smoke test failed: {type(exc).__name__}: {exc}')
		return 1
	print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
