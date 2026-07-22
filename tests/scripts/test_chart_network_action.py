"""Live smoke test for the LLM-assisted chart network action.

This is intentionally a standalone development script.  It is not collected by
pytest and is not part of the default CI suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from playwright.async_api import Browser, async_playwright

from browser_use.webretriever.browser import BrowserRuntime
from browser_use.webretriever.network import ChartNetworkInspector, redact_network_packet
from browser_use.webretriever.runner import RunnerConfig, build_llm

# Keep the requested filename while making the live script opt-in for pytest/CI.
__test__ = False

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPO_ROOT / 'outputs' / 'chart_network_smoke'
JNTO_URL = 'https://statistics.jnto.go.jp/en/graph/#graph--foreigners--by--port--of--entry--by--month'
OWID_URL = 'https://ourworldindata.org/grapher/co-emissions-per-capita?tab=table&time=1916..2000&country=USA~GBR~CHN~ZAF~PAN%5C'
_OWID_DATA_RE = re.compile(r'^https://api\.ourworldindata\.org/v1/indicators/\d+\.data\.json(?:\?.*)?$')


@dataclass(frozen=True, slots=True)
class SmokeCase:
	slug: str
	url: str
	task: str
	wait_description: str
	wait_predicate: Callable[[dict[str, Any]], bool]
	validator: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]


def _first_env(*names: str) -> str | None:
	for name in names:
		value = os.getenv(name)
		if value is not None and value.strip():
			return value.strip()
	return None


def _write_json(path: Path, value: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def _case_logger(case_dir: Path, slug: str) -> logging.Logger:
	logger = logging.getLogger(f'chart-network-smoke.{slug}.{case_dir.parent.name}')
	logger.setLevel(logging.INFO)
	logger.propagate = False
	formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
	file_handler = logging.FileHandler(case_dir / 'run.log', encoding='utf-8')
	file_handler.setFormatter(formatter)
	console_handler = logging.StreamHandler()
	console_handler.setFormatter(formatter)
	logger.addHandler(file_handler)
	logger.addHandler(console_handler)
	return logger


def _packet_has_body(packet: dict[str, Any]) -> bool:
	text = packet.get('response_body')
	encoded = packet.get('response_body_base64')
	return packet.get('response_body_state') == 'complete' and (
		isinstance(text, str) and bool(text) or isinstance(encoded, str) and bool(encoded)
	)


def _validate_jnto(packets: list[dict[str, Any]], matches: list[dict[str, Any]]) -> dict[str, Any]:
	selected_ids = {int(match['request_id']) for match in matches}
	qualified = [
		packet
		for packet in packets
		if int(packet.get('request_id', -1)) in selected_ids
		and packet.get('status') == 200
		and '/vizql/' in str(packet.get('url', ''))
		and _packet_has_body(packet)
	]
	if not qualified:
		raise AssertionError('LLM did not select a non-empty status-200 Tableau /vizql/ data response')
	return {
		'qualified_request_ids': [packet['request_id'] for packet in qualified],
		'qualified_urls': [packet['url'] for packet in qualified],
	}


def _validate_owid(packets: list[dict[str, Any]], matches: list[dict[str, Any]]) -> dict[str, Any]:
	selected_ids = {int(match['request_id']) for match in matches}
	qualified: list[dict[str, Any]] = []
	for packet in packets:
		if int(packet.get('request_id', -1)) not in selected_ids:
			continue
		url = str(packet.get('url', ''))
		if packet.get('status') != 200 or not _OWID_DATA_RE.fullmatch(url):
			continue
		if packet.get('resource_type') != 'other':
			raise AssertionError(f'OWID indicator request unexpectedly had type {packet.get("resource_type")!r}')
		body = packet.get('response_body')
		if not isinstance(body, str) or not body:
			continue
		try:
			decoded = json.loads(body)
		except json.JSONDecodeError as exc:
			raise AssertionError('Selected OWID indicator data response was not valid JSON') from exc
		if decoded:
			qualified.append(packet)
	if not qualified:
		raise AssertionError('LLM did not select a non-empty status-200 OWID indicator *.data.json response')
	return {
		'qualified_request_ids': [packet['request_id'] for packet in qualified],
		'qualified_urls': [packet['url'] for packet in qualified],
		'playwright_resource_types': [packet['resource_type'] for packet in qualified],
	}


CASES = (
	SmokeCase(
		slug='jnto',
		url=JNTO_URL,
		task='Find the network response containing the data for Foreigners Entries by Port of Entry and Month.',
		wait_description='Tableau bootstrapSession response',
		wait_predicate=lambda packet: (
			'/vizql/' in str(packet.get('url', ''))
			and 'bootstrapSession' in str(packet.get('url', ''))
			and packet.get('status') == 200
		),
		validator=_validate_jnto,
	),
	SmokeCase(
		slug='owid',
		url=OWID_URL,
		task='Find the data request used by the current CO₂ emissions per capita table.',
		wait_description='OWID indicator *.data.json response',
		wait_predicate=lambda packet: (bool(_OWID_DATA_RE.fullmatch(str(packet.get('url', '')))) and packet.get('status') == 200),
		validator=_validate_owid,
	),
)


def _model_config(run_dir: Path) -> RunnerConfig:
	model = _first_env('WEBRETRIEVER_MODEL', 'LITELLM_MODEL', 'OPENAI_MODEL')
	api_key = _first_env('WEBRETRIEVER_API_KEY', 'LITELLM_MASTER_KEY', 'OPENAI_API_KEY')
	api_base = _first_env('WEBRETRIEVER_API_BASE', 'LITELLM_BASE_URL', 'OPENAI_BASE_URL')
	if not model:
		raise RuntimeError('Set WEBRETRIEVER_MODEL, LITELLM_MODEL, or OPENAI_MODEL')
	if not api_key:
		raise RuntimeError('Set WEBRETRIEVER_API_KEY, LITELLM_MASTER_KEY, or OPENAI_API_KEY')
	config = RunnerConfig(
		input_path=REPO_ROOT / 'data' / 'data' / 'protocol3.json',
		output_dir=run_dir,
		model=model,
		api_key=api_key,
		api_base=api_base,
		cdp_urls=[],
		api_mode='auto',
		model_timeout_seconds=180.0,
		task_timeout_seconds=600.0,
		max_concurrency=1,
		local_browser=True,
		headless=True,
	)
	config.validate()
	return config


async def _wait_for_captured_request(
	runtime: BrowserRuntime,
	predicate: Callable[[dict[str, Any]], bool],
	*,
	timeout_seconds: float = 60.0,
) -> dict[str, Any]:
	deadline = asyncio.get_running_loop().time() + timeout_seconds
	while True:
		for packet in runtime.current_page_network_requests():
			if predicate(packet):
				return packet
		if asyncio.get_running_loop().time() >= deadline:
			raise TimeoutError(f'network request did not appear within {timeout_seconds:g} seconds')
		await asyncio.sleep(0.25)


async def _prepare_case_page(case: SmokeCase, runtime: BrowserRuntime) -> None:
	page = runtime.page
	if page is None:
		raise RuntimeError('BrowserRuntime did not create an active page')
	if case.slug == 'jnto':
		target = page.locator('#graph--foreigners--by--port--of--entry--by--month')
		await target.wait_for(state='attached', timeout=30_000)
		await target.scroll_into_view_if_needed(timeout=30_000)
		await page.wait_for_timeout(1_500)


def _request_manifest(runtime: BrowserRuntime) -> list[dict[str, Any]]:
	manifest: list[dict[str, Any]] = []
	for packet in runtime.current_page_network_requests():
		redacted = redact_network_packet(packet)
		manifest.append(
			{
				key: redacted.get(key)
				for key in (
					'request_id',
					'timestamp',
					'url',
					'method',
					'resource_type',
					'status',
					'page_url',
					'frame_url',
					'response_body_state',
					'failure',
				)
				if key in redacted
			}
		)
	return manifest


def _reassemble_packets(action_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
	chunks: dict[int, list[tuple[int, str]]] = defaultdict(list)
	expected_chunk_counts: dict[int, int] = {}
	for page in action_pages:
		packet_page = page.get('packet_page')
		if not isinstance(packet_page, dict):
			continue
		request_id = int(packet_page['request_id'])
		chunks[request_id].append((int(packet_page['chunk_index']), str(packet_page['packet_json_chunk'])))
		expected_chunk_counts[request_id] = int(packet_page['chunk_count'])

	packets: list[dict[str, Any]] = []
	for request_id, indexed_chunks in chunks.items():
		if len(indexed_chunks) != expected_chunk_counts[request_id]:
			raise AssertionError(f'incomplete cursor stream for request_id={request_id}')
		ordered = ''.join(value for _, value in sorted(indexed_chunks))
		packet = json.loads(ordered)
		if int(packet.get('request_id', -1)) != request_id:
			raise AssertionError(f'reassembled request_id mismatch for {request_id}')
		packets.append(packet)
	return sorted(packets, key=lambda packet: int(packet['request_id']))


async def _run_case(browser: Browser, llm: Any, run_dir: Path, case: SmokeCase) -> dict[str, Any]:
	case_dir = run_dir / case.slug
	case_dir.mkdir(parents=True, exist_ok=True)
	logger = _case_logger(case_dir, case.slug)
	context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='en-US')
	runtime = BrowserRuntime(
		context,
		case_dir,
		logger,
		navigation_timeout_ms=90_000,
		action_timeout_ms=45_000,
	)
	inspector = ChartNetworkInspector(llm, model_timeout_seconds=180.0)
	stage = 'navigation'
	started_at = datetime.now(timezone.utc)
	try:
		logger.info('START %s %s', case.slug, case.url)
		await runtime.start(case.url)
		stage = 'prepare_page'
		await _prepare_case_page(case, runtime)
		stage = f'wait_for_{case.wait_description}'
		observed_packet = await _wait_for_captured_request(runtime, case.wait_predicate)
		logger.info('Observed prerequisite request_id=%s', observed_packet.get('request_id'))

		page = runtime.page
		if page is None:
			raise RuntimeError('active page disappeared before classification')
		stage = 'llm_classification'
		execution = await inspector.execute(
			runtime=runtime,
			task=case.task,
			page_url=page.url,
			page_title=await page.title(),
		)
		action_pages: list[dict[str, Any]] = []
		cursor: str | None = None
		while True:
			payload = json.loads(execution.output)
			action_pages.append(payload)
			cursor = payload.get('next_cursor')
			if cursor is None:
				break
			stage = 'cursor_pagination'
			execution = await inspector.execute(
				runtime=runtime,
				task=case.task,
				page_url=page.url,
				page_title=await page.title(),
				cursor=cursor,
			)

		stage = 'packet_reassembly'
		packets = _reassemble_packets(action_pages)
		matches = action_pages[0].get('matches', [])
		stage = 'validation'
		validation = case.validator(packets, matches)

		_write_json(case_dir / 'prefilter_requests.json', _request_manifest(runtime))
		_write_json(case_dir / 'deterministic_filtered_requests.json', inspector.last_filtered_requests)
		_write_json(case_dir / 'llm_decisions.json', inspector.last_decisions)
		_write_json(case_dir / 'action_pages.json', action_pages)
		_write_json(case_dir / 'selected_packets.json', packets)
		result = {
			'case': case.slug,
			'status': 'PASS',
			'url': case.url,
			'started_at': started_at.isoformat(),
			'completed_at': datetime.now(timezone.utc).isoformat(),
			'counts': action_pages[0].get('counts', {}),
			'matches': matches,
			'validation': validation,
		}
		_write_json(case_dir / 'result.json', result)
		logger.info('PASS %s selected=%s', case.slug, validation['qualified_request_ids'])
		return result
	except Exception as exc:
		with_context = {
			'case': case.slug,
			'status': 'FAIL',
			'url': case.url,
			'stage': stage,
			'started_at': started_at.isoformat(),
			'failed_at': datetime.now(timezone.utc).isoformat(),
			'error': f'{type(exc).__name__}: {exc}',
			'traceback': ''.join(traceback.format_exception(exc)),
		}
		_write_json(case_dir / 'result.json', with_context)
		_write_json(case_dir / 'prefilter_requests.json', _request_manifest(runtime))
		_write_json(case_dir / 'deterministic_filtered_requests.json', inspector.last_filtered_requests)
		_write_json(case_dir / 'llm_decisions.json', inspector.last_decisions)
		logger.error('FAIL %s at %s: %s', case.slug, stage, with_context['error'])
		return with_context
	finally:
		await runtime.close()
		await context.close()


async def _async_main() -> int:
	load_dotenv(REPO_ROOT / '.env')
	run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
	run_dir = OUTPUT_ROOT / run_id
	run_dir.mkdir(parents=True, exist_ok=True)
	config = _model_config(run_dir)
	llm = build_llm(config)
	results: list[dict[str, Any]] = []
	async with async_playwright() as playwright:
		browser = await playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
		try:
			for case in CASES:
				results.append(await _run_case(browser, llm, run_dir, case))
		finally:
			await browser.close()

	summary = {
		'run_id': run_id,
		'model': config.model,
		'status': 'PASS' if all(result['status'] == 'PASS' for result in results) else 'FAIL',
		'results': results,
	}
	_write_json(run_dir / 'summary.json', summary)
	print(json.dumps(summary, ensure_ascii=False, indent=2))
	return 0 if summary['status'] == 'PASS' else 1


def main() -> int:
	try:
		return asyncio.run(_async_main())
	except KeyboardInterrupt:
		return 130
	except Exception as exc:
		print(f'chart network smoke setup failed: {type(exc).__name__}: {exc}', file=sys.stderr)
		return 2


if __name__ == '__main__':
	raise SystemExit(main())
