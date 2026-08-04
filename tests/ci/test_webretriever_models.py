from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from browser_use.llm.schema import SchemaOptimizer
from browser_use.webretriever.artifacts import (
	MODEL_PROMPT_LOG_FORMAT,
	STRATEGY_REVIEW_PROMPT_LOG_FORMAT,
	TaskArtifactWriter,
	atomic_write_json,
	prepare_task_directory,
	prompt_text_lines,
)
from browser_use.webretriever.models import AgentDecision, CompetitionTask, load_tasks
from browser_use.webretriever.strategy import CHECKPOINT_DECISION_FIELD_LIMITS


def _task(**updates: object) -> CompetitionTask:
	payload: dict[str, object] = {
		'task_idx': 7,
		'task_id': 'safe-task_7',
		'website': 'https://example.com/start',
		'task': 'Find the requested fact.',
		'answer': 'private reference answer',
	}
	payload.update(updates)
	return CompetitionTask.model_validate(payload)


def test_competition_task_drops_ground_truth_from_every_serialization() -> None:
	task = _task(ground_truth='also private', metadata={'split': 'test'})

	assert task.prompt_payload() == {
		'task_idx': 7,
		'task_id': 'safe-task_7',
		'website': 'https://example.com/start',
		'task': 'Find the requested fact.',
	}
	assert task.model_dump() == task.prompt_payload()
	assert 'answer' not in task.model_dump_json()
	assert not hasattr(task, 'answer')


def test_competition_task_accepts_aliases_and_rejects_conflicts() -> None:
	task = CompetitionTask.model_validate(
		{
			'task_index': 2,
			'id': 'abc-2',
			'start_url': 'https://example.org',
			'instruction': ' Read this. ',
		}
	)
	assert task.prompt_payload() == {
		'task_idx': 2,
		'task_id': 'abc-2',
		'website': 'https://example.org',
		'task': 'Read this.',
	}

	with pytest.raises(ValidationError, match='conflicting aliases'):
		CompetitionTask.model_validate(
			{
				'task_idx': 2,
				'task_index': 3,
				'task_id': 'abc',
				'website': 'https://example.org',
				'task': 'Read this.',
			}
		)


@pytest.mark.parametrize('task_id', ['../escape', 'a/b', r'a\b', '.hidden', 'has space', 'x..y'])
def test_competition_task_rejects_unsafe_ids(task_id: str) -> None:
	with pytest.raises(ValidationError, match='task_id'):
		_task(task_id=task_id)


@pytest.mark.parametrize(
	'website', ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.com', 'example.com']
)
def test_competition_task_rejects_unsafe_start_urls(website: str) -> None:
	with pytest.raises(ValidationError, match='website'):
		_task(website=website)


def test_load_tasks_supports_array_wrapper_jsonl_and_single_record(tmp_path: Path) -> None:
	records = [
		{
			'task_idx': index,
			'task_id': f'id-{index}',
			'website': 'https://example.com',
			'task': f'task {index}',
			'answer': f'secret {index}',
		}
		for index in range(2)
	]
	array_path = tmp_path / 'array.json'
	array_path.write_text(json.dumps(records), encoding='utf-8')
	wrapper_path = tmp_path / 'wrapper.json'
	wrapper_path.write_text(json.dumps({'version': 1, 'tasks': records}), encoding='utf-8')
	jsonl_path = tmp_path / 'tasks.jsonl'
	jsonl_path.write_text('\n'.join(json.dumps(record) for record in records), encoding='utf-8')
	single_path = tmp_path / 'single.jsonl'
	single_path.write_text(json.dumps(records[0]), encoding='utf-8')

	for path, expected_count in (
		(array_path, 2),
		(wrapper_path, 2),
		(jsonl_path, 2),
		(single_path, 1),
	):
		tasks = load_tasks(path)
		assert len(tasks) == expected_count
		assert all('secret' not in task.model_dump_json() for task in tasks)


@pytest.mark.parametrize(
	'field,duplicate_value',
	[
		('task_idx', 0),
		('task_id', 'id-0'),
	],
)
def test_load_tasks_rejects_duplicate_indices_and_ids(tmp_path: Path, field: str, duplicate_value: object) -> None:
	records = [
		{'task_idx': 0, 'task_id': 'id-0', 'website': 'https://example.com', 'task': 'one'},
		{'task_idx': 1, 'task_id': 'id-1', 'website': 'https://example.com', 'task': 'two'},
	]
	records[1][field] = duplicate_value
	path = tmp_path / 'duplicate.json'
	path.write_text(json.dumps(records), encoding='utf-8')

	with pytest.raises(ValueError, match=f'duplicate {field}'):
		load_tasks(path)


@pytest.mark.parametrize(
	'payload',
	[
		{'action': 'click', 'element_id': 1},
		{'action': 'double_click', 'element_id': 1},
		{'action': 'type', 'element_id': 1, 'text': 'value'},
		{'action': 'select', 'element_id': 1, 'text': 'label'},
		{'action': 'press', 'key': 'Enter', 'element_id': 1},
		{'action': 'scroll', 'direction': 'down', 'pages': 2, 'element_id': 1},
		{'action': 'hover', 'element_id': 1},
		{'action': 'click_xy', 'x': 10, 'y': 20},
		{'action': 'hover_xy', 'x': 10, 'y': 20},
		{'action': 'drag', 'x': 10, 'y': 20, 'end_x': 30, 'end_y': 40},
		{'action': 'back'},
		{'action': 'navigate', 'url': 'https://example.com/next'},
		{'action': 'wait', 'seconds': 1.5},
		{'action': 'switch_tab', 'tab_index': 1},
		{'action': 'close_tab', 'tab_index': 1},
		{'action': 'read_element', 'element_id': 1},
		{'action': 'find_text', 'text': 'needle'},
		{'action': 'inspect_network'},
		{'action': 'inspect_network', 'text': '/api/data'},
		{'action': 'inspect_network', 'request_id': 42},
		{'action': 'inspect_network', 'text': 'revenue', 'request_id': 42},
		{'action': 'inspect_network', 'request_id': 42, 'network_cursor': 'opaque-page-2'},
		{'action': 'find_chart_data_requests'},
		{'action': 'find_chart_data_requests', 'chart_cursor': 'scan-id:1'},
		{
			'action': 'call_data_analysis_assistant',
			'analysis_query': 'Which month has the highest ratio?',
			'data_dir': '/tmp/task/chart_data/scan-id',
		},
		{'action': 'calculate', 'operation': 'argmax_growth', 'text': '{"2023":10,"2024":12}'},
		{'action': 'finish', 'success': True, 'answer': '42', 'evidence': ['The page displays 42.']},
	],
)
def test_agent_decision_supports_every_flat_action(payload: dict[str, object]) -> None:
	decision = AgentDecision.model_validate({'thought': 'next', 'memory': 'fact', **payload})
	assert decision.action_payload()['action'] == payload['action']


def test_agent_decision_exposes_nullable_flat_checkpoint_fields_to_strict_providers() -> None:
	schema = SchemaOptimizer.create_optimized_json_schema(AgentDecision)

	for name in (
		'checkpoint_strategy_catalog',
		'checkpoint_active_strategy',
		'checkpoint_confirmed_infeasible',
		'checkpoint_next_strategies',
	):
		assert name in schema['required']
		assert {'type': 'null'} in schema['properties'][name]['anyOf']
		string_schema = next(option for option in schema['properties'][name]['anyOf'] if option['type'] == 'string')
		assert string_schema['maxLength'] == CHECKPOINT_DECISION_FIELD_LIMITS[name]
		with pytest.raises(ValidationError):
			AgentDecision(action='wait', seconds=0.1, **{name: 'x' * (CHECKPOINT_DECISION_FIELD_LIMITS[name] + 1)})

	assert all(
		'Markdown list' in schema['properties'][name]['description']
		for name in (
			'checkpoint_strategy_catalog',
			'checkpoint_active_strategy',
			'checkpoint_confirmed_infeasible',
			'checkpoint_next_strategies',
		)
	)

	decision = AgentDecision(action='wait', seconds=0.1, checkpoint_strategy_catalog='Table and export routes.')
	assert decision.checkpoint_strategy_catalog == 'Table and export routes.'
	assert all(name not in decision.action_payload() for name in schema['properties'] if name.startswith('checkpoint_'))


def test_agent_decision_preserves_scoped_network_search_fields() -> None:
	decision = AgentDecision.model_validate({'action': 'inspect_network', 'text': 'revenue', 'request_id': 42})
	payload = decision.action_payload()

	assert decision.text == 'revenue'
	assert decision.request_id == 42
	assert payload['text'] == 'revenue'
	assert payload['request_id'] == 42


def test_agent_decision_flattens_nested_gateway_action() -> None:
	decision = AgentDecision.model_validate(
		{
			'thought': 'Use the date picker.',
			'memory': 'The historical list is open.',
			'action': {'action': 'click_xy', 'x': 171, 'y': 92},
		}
	)

	assert decision.action == 'click_xy'
	assert decision.x == 171
	assert decision.y == 92


def test_agent_decision_flattens_action_named_gateway_envelope() -> None:
	decision = AgentDecision.model_validate(
		{
			'thought': 'Search the indicator catalog.',
			'memory': 'World Bank is open.',
			'type': {'element_id': 8, 'text': 'Adolescent fertility rate'},
		}
	)

	assert decision.action == 'type'
	assert decision.element_id == 8
	assert decision.text == 'Adolescent fertility rate'


@pytest.mark.parametrize(
	'action,typed_field',
	[
		('inspect_network', 'network_cursor'),
		('find_chart_data_requests', 'chart_cursor'),
	],
)
def test_agent_decision_maps_legacy_cursor_to_its_typed_action_field(action: str, typed_field: str) -> None:
	payload: dict[str, object] = {'action': action, 'cursor': 'legacy-page-2'}
	if action == 'inspect_network':
		payload['request_id'] = 42

	decision = AgentDecision.model_validate(payload)

	assert getattr(decision, typed_field) == 'legacy-page-2'
	assert 'cursor' not in AgentDecision.model_json_schema()['properties']
	assert 'cursor' not in decision.action_payload()


@pytest.mark.parametrize(
	'payload',
	[
		{'action': 'inspect_network', 'request_id': 42, 'chart_cursor': 'wrong-tool'},
		{'action': 'find_chart_data_requests', 'network_cursor': 'wrong-tool'},
		{'action': 'click', 'cursor': 'wrong-tool', 'element_id': 1},
	],
)
def test_agent_decision_rejects_cross_tool_cursors(payload: dict[str, object]) -> None:
	with pytest.raises(ValidationError):
		AgentDecision.model_validate(payload)


def test_agent_decision_rejects_conflicting_nested_gateway_action() -> None:
	with pytest.raises(ValidationError, match='conflicting nested action field: x'):
		AgentDecision.model_validate(
			{
				'action': {'action': 'click_xy', 'x': 171, 'y': 92},
				'x': 999,
			}
		)


@pytest.mark.parametrize(
	'payload',
	[
		{'action': 'click'},
		{'action': 'click', 'element_id': 1, 'text': 'unexpected'},
		{'action': 'navigate', 'url': 'ftp://example.com'},
		{'action': 'wait', 'seconds': 31.0},
		{'action': 'finish', 'success': True, 'answer': '42'},
		{'action': 'finish', 'success': True, 'evidence': ['fact']},
		{'action': 'finish', 'success': True, 'answer': '42', 'evidence': 'not-a-list'},
		{'action': 'call_data_analysis_assistant', 'analysis_query': 'Analyze this.'},
		{'action': 'call_data_analysis_assistant', 'data_dir': '/tmp/task/chart_data/scan-id'},
		{
			'action': 'call_data_analysis_assistant',
			'analysis_query': 'Analyze this.',
			'data_dir': '/tmp/task/chart_data/scan-id',
			'cursor': 'not-accepted',
		},
		{'action': 'call_data_analysis_assistant', 'analysis_query': '   ', 'data_dir': '/tmp/data'},
		{'action': 'find_chart_data_requests', 'analysis_query': 'Analyze this.'},
		{'action': 'inspect_network', 'text': 'revenue', 'request_id': 42, 'network_cursor': 'opaque-page-2'},
		{'action': 'inspect_network', 'text': 'revenue', 'request_id': 42, 'cursor': 'opaque-page-2'},
		{'action': 'inspect_network', 'cursor': 'opaque-page-2'},
	],
)
def test_agent_decision_rejects_missing_or_irrelevant_parameters(payload: dict[str, object]) -> None:
	with pytest.raises(ValidationError):
		AgentDecision.model_validate(payload)


def test_atomic_write_json_preserves_previous_file_on_serialization_failure(tmp_path: Path) -> None:
	path = tmp_path / 'nested' / 'result.json'
	atomic_write_json(path, {'answer': '中文'})
	assert json.loads(path.read_text(encoding='utf-8')) == {'answer': '中文'}

	with pytest.raises(TypeError):
		atomic_write_json(path, {'bad': object()})
	assert json.loads(path.read_text(encoding='utf-8')) == {'answer': '中文'}
	assert not list(path.parent.glob(f'.{path.name}.*.tmp'))


def test_task_artifacts_create_scaffolds_and_update_atomically(tmp_path: Path) -> None:
	task = _task()
	task_dir = prepare_task_directory(tmp_path, task)
	writer = TaskArtifactWriter(tmp_path, task)

	assert task_dir == tmp_path / '7_safe-task_7'
	assert writer.trajectory_dir.is_dir()
	assert writer.trajectory_visual_dir.is_dir()
	assert writer.logs_dir.is_dir()
	assert writer.lock_path.is_file()
	result = json.loads(writer.result_path.read_text(encoding='utf-8'))
	assert result['status'] == 'PENDING'
	assert 'answer' not in result
	assert json.loads(writer.capture_path.read_text(encoding='utf-8'))['all_requests'] == []
	prompt_log = json.loads(writer.model_prompt_log_path.read_text(encoding='utf-8'))
	assert prompt_log == {'format': MODEL_PROMPT_LOG_FORMAT, 'system_prompt': [], 'steps': []}
	strategy_review_prompt_log = json.loads(writer.strategy_review_prompt_log_path.read_text(encoding='utf-8'))
	assert strategy_review_prompt_log == {
		'format': STRATEGY_REVIEW_PROMPT_LOG_FORMAT,
		'system_prompt': [],
		'reviews': [],
	}
	assert 'private reference answer' not in json.dumps(prompt_log, ensure_ascii=False)
	assert prompt_text_lines('first\n\nthird\n') == ['first', '', 'third', '']
	assert '\n'.join(prompt_text_lines('first\n\nthird\n')) == 'first\n\nthird\n'

	writer.write_result(
		status='SUCCESS',
		agent_answer='agent result',
		answer='private reference must disappear',
		actions=['click'],
		ground_truth='must disappear',
	)
	result = json.loads(writer.result_path.read_text(encoding='utf-8'))
	assert result['agent_answer'] == 'agent result'
	assert result['status'] == 'SUCCESS'
	assert 'ground_truth' not in result

	# Preparation is resumable and must not clobber a terminal result.
	prepare_task_directory(tmp_path, task)
	assert json.loads(writer.result_path.read_text(encoding='utf-8'))['status'] == 'SUCCESS'

	written_capture = writer.write_capture([{'url': 'https://example.com/api', 'method': 'GET'}])
	assert json.loads(written_capture.read_text(encoding='utf-8'))['total_requests'] == 1
	assert writer.trajectory_path(3) == writer.trajectory_dir / '3.png'
	assert writer.trajectory_path(3, visual=True) == writer.trajectory_visual_dir / '3.png'


def test_task_lock_uses_advisory_ownership_not_file_existence(tmp_path: Path) -> None:
	task = _task()
	first = TaskArtifactWriter(tmp_path, task)
	second = TaskArtifactWriter(tmp_path, task)
	first.prepare()

	assert first.acquire_lock()
	assert not second.acquire_lock()
	first.release_lock()
	assert first.lock_path.exists()
	assert second.acquire_lock()
	second.release_lock()
