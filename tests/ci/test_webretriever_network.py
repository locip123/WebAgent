from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from browser_use.webretriever.network import (
	ChartNetworkInspector,
	ChartRequestBatchDecision,
	ChartRequestDecision,
	_packet_classifier_descriptor,
	_selected_request_ids,
)


class FakeRuntime:
	def __init__(self, captured: list[dict[str, Any]], materialized: dict[int, dict[str, Any]]) -> None:
		self.captured = captured
		self.materialized = materialized
		self.settle_calls = 0
		self.materialize_calls: list[tuple[int, int]] = []

	async def settle_network_capture(self) -> None:
		self.settle_calls += 1

	def current_page_network_requests(self) -> list[dict[str, Any]]:
		return [dict(packet) for packet in self.captured]

	async def materialize_network_request(self, request_id: int, *, max_body_bytes: int) -> dict[str, Any]:
		self.materialize_calls.append((request_id, max_body_bytes))
		return dict(self.materialized[request_id])


class FakeClassifierLLM:
	def __init__(self, decisions: list[ChartRequestDecision]) -> None:
		self.decisions = decisions
		self.calls: list[tuple[list[Any], Any]] = []

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		return SimpleNamespace(
			completion=ChartRequestBatchDecision(decisions=self.decisions),
			usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5, total_tokens=18),
		)


class SlowClassifierLLM:
	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		await asyncio.sleep(10)
		raise AssertionError('unreachable')


def _request(request_id: int, url: str, *, timestamp: float | None = None) -> dict[str, Any]:
	return {
		'request_id': request_id,
		'timestamp': float(request_id) if timestamp is None else timestamp,
		'url': url,
		'method': 'POST',
		'headers': {'Authorization': 'Bearer request-secret'},
		'resource_type': 'xhr',
		'post_data': 'worksheet=Port+of+Entry&token=request-secret',
		'page_id': 1,
		'document_generation': 2,
		'page_url': 'https://example.test/chart',
		'frame_url': 'https://embed.example.test/chart',
		'status': 200,
		'response_headers': {'content-type': 'application/json'},
		'response_body_state': 'available',
	}


def test_selected_request_ids_adds_owid_and_tableau_companions() -> None:
	packets = [
		_request(1, 'https://api.ourworldindata.org/v1/indicators/123.data.json'),
		_request(2, 'https://api.ourworldindata.org/v1/indicators/123.metadata.json'),
		_request(3, 'https://api.ourworldindata.org/v1/indicators/999.metadata.json'),
	]
	decision = ChartRequestDecision(request_id=1, contains_chart_data=True, relevance='target', reason='target data')

	assert _selected_request_ids(packets, [decision]) == [1, 2]

	tableau = [
		_request(10, 'https://public.tableau.com/vizql/w/book/v/view/bootstrapSession/sessions/session-a'),
		_request(11, 'https://public.tableau.com/vizql/w/book/v/view/sessions/session-a/commands/tabdoc/filter'),
		_request(12, 'https://public.tableau.com/vizql/w/book/v/view/sessions/session-b/commands/tabdoc/filter'),
	]
	tableau_decision = ChartRequestDecision(
		request_id=11, contains_chart_data=True, relevance='target', reason='target worksheet'
	)

	assert _selected_request_ids(tableau, [tableau_decision]) == [10, 11]
	descriptor = json.dumps(_packet_classifier_descriptor(tableau[0]), ensure_ascii=False)
	assert 'session-a' not in descriptor
	assert 'request-secret' not in descriptor

	noisy_session = [
		_request(100, 'https://public.tableau.com/vizql/w/book/v/view/bootstrapSession/sessions/session-c'),
		_request(
			101,
			'https://public.tableau.com/vizql/w/book/v/view/sessions/session-c/commands/tabdoc/categorical-filter-by-index',
		),
		*[
			_request(
				request_id,
				'https://public.tableau.com/vizql/w/book/v/view/sessions/session-c/commands/tabsrv/get-filter-info',
			)
			for request_id in range(102, 122)
		],
	]
	noisy_decision = ChartRequestDecision(
		request_id=121, contains_chart_data=True, relevance='target', reason='current worksheet metadata'
	)
	selected = _selected_request_ids(noisy_session, [noisy_decision])
	assert len(selected) == 16
	assert selected == sorted(selected)
	assert {100, 101, 121} <= set(selected)


@pytest.mark.asyncio
async def test_find_action_saves_sanitized_artifact_and_cursor_reassembles_saved_packet(tmp_path: Path) -> None:
	captured = _request(7, 'https://example.test/chart.json?year=2023&api_key=url-secret')
	body = json.dumps(
		{
			'rows': [{'month': 'Nov', 'value': 42, 'note': 'x' * 9_000}],
			'token': 'body-secret',
		},
		ensure_ascii=False,
	)
	complete = {
		**captured,
		'response_body_state': 'complete',
		'response_body_bytes': len(body.encode()),
		'response_body': body,
		'response_json': json.loads(body),
	}
	runtime = FakeRuntime([captured], {7: complete})
	llm = FakeClassifierLLM(
		[ChartRequestDecision(request_id=7, contains_chart_data=True, relevance='target', reason='target chart rows')]
	)
	inspector = ChartNetworkInspector(llm, model_timeout_seconds=1)
	task_dir = tmp_path / '4_task'
	identity = {'task_idx': 4, 'task_id': 'task', 'website': 'https://example.test/chart', 'task': 'Find November'}

	execution = await inspector.execute(
		runtime=runtime,
		task='Find the maximum month',
		page_url='https://example.test/chart?session=page-secret',
		page_title='Chart',
		task_dir=task_dir,
		task_identity=identity,
	)
	payload = json.loads(execution.output)

	assert payload['status'] == 'ready'
	assert payload['counts']['matched'] == 1
	assert payload['counts']['sent_to_llm'] == 1
	assert payload['active_filters'] == {'page_title': 'Chart'}
	assert payload['datasets'][0]['active_filters'] == {'page_title': 'Chart'}
	assert execution.usage == {'prompt_tokens': 13, 'completion_tokens': 5, 'total_tokens': 18}
	assert len(execution.output.encode('utf-8')) <= 32 * 1024
	assert len(payload['packet_preview']['text'].encode('utf-8')) <= 8 * 1024
	assert runtime.settle_calls == 1
	assert len(runtime.materialize_calls) == 1
	assert len(llm.calls) == 1
	classifier_prompt = str(llm.calls[0][0])
	assert 'url-secret' not in classifier_prompt
	assert 'request-secret' not in classifier_prompt
	assert 'body-secret' not in classifier_prompt
	assert 'page-secret' not in classifier_prompt
	assert '<redacted>' in classifier_prompt

	data_dir = Path(payload['data_dir'])
	assert data_dir.parent == (task_dir / 'chart_data').resolve()
	manifest_path = data_dir / 'manifest.json'
	manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
	assert manifest['complete'] is True
	assert manifest['task_identity'] == identity
	assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == payload['manifest_sha256']
	packet_entry = manifest['packets'][0]
	metadata_path = data_dir / packet_entry['metadata_path']
	body_path = data_dir / packet_entry['body_path']
	for secret in ('url-secret', 'request-secret', 'body-secret', 'page-secret'):
		assert secret not in metadata_path.read_text(encoding='utf-8')
		assert secret not in body_path.read_text(encoding='utf-8')
		assert secret not in execution.output
	assert hashlib.sha256(body_path.read_bytes()).hexdigest() == packet_entry['body_sha256']
	assert payload['packet_preview']['next_cursor'] == payload['next_cursor']

	chunks: list[str] = []
	first_cursor = payload['next_cursor']
	cursor = first_cursor
	while cursor:
		page_execution = await inspector.execute(
			runtime=runtime,
			task='unused for cursor reads',
			page_url='https://example.test/chart',
			cursor=cursor,
		)
		page = json.loads(page_execution.output)
		assert len(page_execution.output.encode('utf-8')) <= 32 * 1024
		for secret in ('url-secret', 'request-secret', 'body-secret'):
			assert secret not in page_execution.output
		chunks.append(page['packet_page']['packet_json_chunk'])
		cursor = page['next_cursor']
	packet = json.loads(''.join(chunks))
	assert packet['response_body'].encode('utf-8') == body_path.read_bytes()
	assert runtime.settle_calls == 1
	assert len(llm.calls) == 1

	body_path.write_text('tampered after scan', encoding='utf-8')
	stale = json.loads(
		(
			await inspector.execute(
				runtime=runtime,
				task='unused for cursor reads',
				page_url='https://example.test/chart',
				cursor=first_cursor,
			)
		).output
	)
	assert stale['status'] == 'stale_state'
	assert 'checksum' in stale['error']


@pytest.mark.asyncio
async def test_find_action_reports_capture_pending_timeout_and_stale_cursor(tmp_path: Path) -> None:
	captured = _request(3, 'https://example.test/data.json')
	pending_runtime = FakeRuntime([captured], {3: {**captured, 'response_body_state': 'pending'}})
	decision = ChartRequestDecision(request_id=3, contains_chart_data=True, relevance='target', reason='likely data')
	pending = ChartNetworkInspector(FakeClassifierLLM([decision]), model_timeout_seconds=1)
	pending_payload = json.loads(
		(
			await pending.execute(
				runtime=pending_runtime,
				task='Analyze chart',
				page_url='https://example.test/chart',
				task_dir=tmp_path / 'pending-task',
				task_identity={'task_id': 'pending-task'},
			)
		).output
	)
	assert pending_payload['status'] == 'capture_pending'

	too_large_runtime = FakeRuntime(
		[captured],
		{
			3: {
				**captured,
				'response_body_state': 'body_too_large',
				'response_body_bytes': 26 * 1024 * 1024,
				'response_error': 'content-length exceeds limit',
			}
		},
	)
	too_large = ChartNetworkInspector(FakeClassifierLLM([decision]), model_timeout_seconds=1)
	too_large_payload = json.loads(
		(
			await too_large.execute(
				runtime=too_large_runtime,
				task='Analyze chart',
				page_url='https://example.test/chart',
				task_dir=tmp_path / 'large-task',
				task_identity={'task_id': 'large-task'},
			)
		).output
	)
	assert too_large_payload['status'] == 'too_large'

	timeout = ChartNetworkInspector(SlowClassifierLLM(), model_timeout_seconds=0.01)
	timeout_payload = json.loads(
		(
			await timeout.execute(
				runtime=FakeRuntime([captured], {3: captured}),
				task='Analyze chart',
				page_url='https://example.test/chart',
			)
		).output
	)
	assert timeout_payload['status'] == 'timeout'

	stale_payload = json.loads(
		(
			await pending.execute(
				runtime=pending_runtime,
				task='unused',
				page_url='https://example.test/chart',
				cursor='expired:0',
			)
		).output
	)
	assert stale_payload['status'] == 'stale_state'
