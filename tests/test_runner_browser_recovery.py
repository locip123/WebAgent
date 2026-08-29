from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.runner import RunnerConfig, run


async def _serve_page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
	await reader.read(65_536)
	body = b'<html><body>runner-recovery</body></html>'
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


def test_dead_cdp_worker_does_not_consume_the_remaining_task_queue(tmp_path: Path) -> None:
	async def scenario() -> dict[str, object]:
		server = await asyncio.start_server(_serve_page, '127.0.0.1', 0)
		website = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
		task_file = tmp_path / 'tasks.json'
		task_file.write_text(
			json.dumps(
				[
					{'task_idx': index, 'task_id': f'task-{index}', 'website': website, 'task': 'probe'}
					for index in range(3)
				]
			),
			encoding='utf-8',
		)
		with tempfile.TemporaryDirectory(prefix='wr-runner-recovery-') as profile:
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
				admin = None
				try:
					endpoint = await _wait_for_devtools_port(profile_dir)
					admin = await playwright.chromium.connect_over_cdp(endpoint)
					terminated = False

					def terminate_after_first_task_page_closes(page) -> None:
						nonlocal terminated
						if terminated:
							return

						def terminate() -> None:
							nonlocal terminated
							if terminated or process.returncode is not None:
								return
							terminated = True
							process.terminate()

						page.on('close', terminate)

					admin.contexts[0].on('page', terminate_after_first_task_page_closes)
					config = RunnerConfig(
						input_path=task_file,
						output_dir=tmp_path / 'output',
						model='test-model',
						cdp_urls=[endpoint],
						model_services=[
							ModelServiceConfig('unavailable-model', 'http://127.0.0.1:1/v1', 'test-key')
						],
						max_steps=1,
						model_timeout_seconds=0.1,
						task_timeout_seconds=0.5,
						max_concurrency=1,
					)
					summary = await run(config)
					return summary
				finally:
					if admin is not None:
						with contextlib.suppress(Exception):
							await admin.close()
					if process.returncode is None:
						process.terminate()
					await process.wait()
		server.close()
		await server.wait_closed()

		return {}

	summary = asyncio.run(scenario())
	statuses = summary['statuses']
	assert statuses['task-1'] == 'FAIL_TASK_TIMEOUT'
	assert statuses['task-2'] == 'FAIL_BROWSER_CONNECT'
	assert 'FAIL_RUNTIME' not in statuses.values()
