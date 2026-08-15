from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from browser_use.webretriever import cli
from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
from browser_use.webretriever.artifacts import TaskArtifactWriter
from browser_use.webretriever.configuration import ConfigurationError, load_file_configuration
from browser_use.webretriever.connection import BrowserDriver
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE
from browser_use.webretriever.runner import (
	RunnerConfig,
	_consume_tasks,
	_experiment_records_from_artifacts,
	_result_payload,
	_run_task,
	_task_timeout_outcome,
	build_llm,
	validate_model_policy,
)


def _config(tmp_path: Path, **overrides: object) -> RunnerConfig:
	values: dict[str, object] = {
		'input_path': tmp_path / 'tasks.json',
		'output_dir': tmp_path / 'output',
		'model': 'gpt-5.4',
		'api_key': 'test-key',
		'api_base': 'https://api.example.test/v1',
		'cdp_urls': ['ws://browser.example.test/devtools/browser/one'],
	}
	values.update(overrides)
	return RunnerConfig(**values)  # type: ignore[arg-type]


def _write_task_file(path: Path) -> None:
	path.write_text(
		json.dumps(
			[
				{
					'task_idx': 0,
					'task_id': '0123456789abcdef0123456789abcdef',
					'website': 'https://example.com',
					'task': 'Read the result.',
					'answer': 'must-not-appear-in-summary',
				}
			]
		),
		encoding='utf-8',
	)


def test_cli_validate_only_accepts_input_and_output_aliases_without_credentials(tmp_path: Path, capsys):
	input_path = tmp_path / 'tasks.json'
	_write_task_file(input_path)

	exit_code = cli.main(
		[
			'--task_file',
			str(input_path),
			'--output-dir',
			str(tmp_path / 'unused-output'),
			'--validate-only',
		]
	)

	assert exit_code == 0
	summary = json.loads(capsys.readouterr().out)
	assert summary['task_count'] == 1
	assert summary['task_indices'] == [0]
	assert summary['ground_truth_exposed_to_agent'] is False
	assert 'must-not-appear-in-summary' not in json.dumps(summary)


@pytest.mark.parametrize('input_flag', ['--input', '--task-file', '--task_file'])
@pytest.mark.parametrize('output_flag', ['--output', '--output-dir', '--output_dir'])
def test_cli_parser_accepts_task_and_output_aliases(tmp_path: Path, input_flag: str, output_flag: str):
	args = cli.build_parser().parse_args(
		[
			input_flag,
			str(tmp_path / 'tasks.json'),
			output_flag,
			str(tmp_path / 'output'),
			'--validate-only',
		]
	)

	assert args.input_path == tmp_path / 'tasks.json'
	assert args.output_dir == tmp_path / 'output'
	assert args.max_concurrency == 3


def test_cli_loads_toml_configuration_and_cli_overrides_task_selection(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'environment-model-that-must-not-win')
	config_path = tmp_path / 'webretriever.toml'
	config_path.write_text(
		'''[webretriever]
input_path = "tasks.json"
output_dir = "output"
model = "gpt-5.4"
api_key = "config-key"
api_base = "https://gateway.example.test/v1"
api_mode = "responses"
reasoning_effort = "low"
thought_language = "中文"
max_steps = 99
model_timeout_seconds = 120.0
task_timeout_seconds = 300.0
max_concurrency = 2
structured_prompt_log = true
local_browser = true
headless = false
rerun_failed = true
cdp_urls = []
vlm_ports = []
task_indices = ["1,4-6"]
limit = 5
validate_only = false
''',
		encoding='utf-8',
	)

	configuration = load_file_configuration(config_path)
	parser = cli.build_parser(configuration)
	args = parser.parse_args(['--task-index', '9', '--no-headed'])
	config = cli.config_from_args(args, parser)

	assert config.input_path == Path('tasks.json')
	assert config.output_dir == Path('output')
	assert config.model == 'gpt-5.4'
	assert config.api_key == 'config-key'
	assert config.api_mode == 'responses'
	assert config.max_steps == 99
	assert config.model_timeout_seconds == 120
	assert config.task_timeout_seconds == 300
	assert config.max_concurrency == 2
	assert config.structured_prompt_log is True
	assert config.local_browser is True
	assert config.headless is True
	assert config.rerun_failed is True
	assert config.task_indices == frozenset({9})
	assert config.limit == 5


def test_cli_validate_only_reads_input_path_from_toml(tmp_path: Path, capsys):
	input_path = tmp_path / 'tasks.json'
	_write_task_file(input_path)
	config_path = tmp_path / 'webretriever.toml'
	config_path.write_text(f'[webretriever]\ninput_path = "{input_path}"\nvalidate_only = true\n', encoding='utf-8')

	exit_code = cli.main(['--config', str(config_path)])

	assert exit_code == 0
	assert json.loads(capsys.readouterr().out)['task_count'] == 1


def test_toml_configuration_rejects_unknown_or_mistyped_settings(tmp_path: Path):
	config_path = tmp_path / 'webretriever.toml'
	config_path.write_text('[webretriever]\nunknown_setting = true\n', encoding='utf-8')

	with pytest.raises(ConfigurationError, match='unsupported'):
		load_file_configuration(config_path)

	config_path.write_text('[webretriever]\nmax_steps = "100"\n', encoding='utf-8')
	with pytest.raises(ConfigurationError, match='must be an integer'):
		load_file_configuration(config_path)


def test_cli_config_uses_environment_and_cdp_alias(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'gpt-5.4')
	monkeypatch.setenv('WEBRETRIEVER_API_KEY', 'web-key')
	monkeypatch.setenv('WEBRETRIEVER_API_BASE', 'https://gateway.example.test/v1')
	monkeypatch.setenv('WEBRETRIEVER_CDP_URLS', 'ws://ignored.example.test/from-env')
	monkeypatch.setenv('LITELLM_MODEL', 'should-not-win')
	monkeypatch.setenv('OPENAI_API_KEY', 'should-not-win')
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--task-file',
			str(tmp_path / 'tasks.json'),
			'--output_dir',
			str(tmp_path / 'output'),
			'--cdp-url',
			'ws://browser.example.test/one,ws://browser.example.test/two',
		]
	)

	config = cli.config_from_args(args, parser)

	assert config.model == 'gpt-5.4'
	assert config.api_key == 'web-key'
	assert config.api_base == 'https://gateway.example.test/v1'
	assert config.cdp_urls == [
		'ws://browser.example.test/one',
		'ws://browser.example.test/two',
	]
	assert config.thought_language == DEFAULT_THOUGHT_LANGUAGE
	assert config.structured_prompt_log is False


def test_cli_config_allows_thought_language_override(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'gpt-5.4')
	monkeypatch.setenv('WEBRETRIEVER_API_KEY', 'web-key')
	monkeypatch.setenv('WEBRETRIEVER_THOUGHT_LANGUAGE', '日本語')
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
			'--cdp-url',
			'ws://browser.example.test/one',
			'--thought-language',
			'English',
		]
	)

	config = cli.config_from_args(args, parser)

	assert config.thought_language == 'English'


def test_cli_config_enables_structured_prompt_log_only_when_requested(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'gpt-5.4')
	monkeypatch.setenv('WEBRETRIEVER_API_KEY', 'web-key')
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
			'--cdp-url',
			'ws://browser.example.test/one',
			'--structured-prompt-log',
		]
	)

	assert cli.config_from_args(args, parser).structured_prompt_log is True


def test_cli_config_reads_cdp_urls_from_environment(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'gpt-5.4')
	monkeypatch.setenv('WEBRETRIEVER_API_KEY', 'web-key')
	monkeypatch.setenv(
		'WEBRETRIEVER_CDP_URLS',
		'ws://browser.example.test/one, ws://browser.example.test/two ws://browser.example.test/one',
	)
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
		]
	)

	config = cli.config_from_args(args, parser)

	assert config.cdp_urls == [
		'ws://browser.example.test/one',
		'ws://browser.example.test/two',
	]


def test_cli_config_reads_declared_sec_user_agent_from_environment(monkeypatch, tmp_path: Path):
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'gpt-5.4')
	monkeypatch.setenv('WEBRETRIEVER_API_KEY', 'web-key')
	monkeypatch.setenv('WEBRETRIEVER_SEC_USER_AGENT', 'Example Organization sec-admin@example.org')
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
			'--cdp-url',
			'ws://browser.example.test/one',
		]
	)

	config = cli.config_from_args(args, parser)

	assert config.sec_user_agent == 'Example Organization sec-admin@example.org'


def test_cli_vlm_ports_compatibility_ignores_gateway_environment(monkeypatch, tmp_path: Path):
	for name in ('WEBRETRIEVER_API_KEY', 'LITELLM_MASTER_KEY', 'OPENAI_API_KEY'):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv('WEBRETRIEVER_MODEL', 'local-model')
	monkeypatch.setenv('LITELLM_BASE_URL', 'https://gateway.example.test/v1')
	parser = cli.build_parser()
	args = parser.parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
			'--cdp_url',
			'ws://browser.example.test/one',
			'--vlm_ports',
			'8000',
			'8001',
		]
	)

	config = cli.config_from_args(args, parser)
	second_worker_llm = build_llm(config, worker_id=1)

	assert config.api_base is None
	assert config.api_key == ''
	assert config.vlm_ports == [8000, 8001]
	assert str(second_worker_llm.base_url) == 'http://127.0.0.1:8001/v1'
	assert second_worker_llm.use_responses_api is False
	assert second_worker_llm.stream_responses_api is False


def test_gateway_responses_mode_uses_terminal_event_streaming(tmp_path: Path):
	llm = build_llm(_config(tmp_path, api_mode='responses'))

	assert llm.use_responses_api is True
	assert llm.stream_responses_api is True


@pytest.mark.parametrize('cdp_flag', ['--cdp_url', '--cdp-url', '--cdp-urls'])
def test_cli_parser_accepts_cdp_url_aliases(tmp_path: Path, cdp_flag: str):
	args = cli.build_parser().parse_args(
		[
			'--input',
			str(tmp_path / 'tasks.json'),
			'--output',
			str(tmp_path / 'output'),
			cdp_flag,
			'ws://browser.example.test/one',
			'ws://browser.example.test/two',
		]
	)

	assert args.cdp_urls == [
		'ws://browser.example.test/one',
		'ws://browser.example.test/two',
	]


def test_runner_config_accepts_exact_competition_limits(tmp_path: Path):
	config = _config(
		tmp_path,
		max_steps=100,
		model_timeout_seconds=180,
		max_concurrency=8,
		cdp_urls=[f'ws://browser.example.test/{index}' for index in range(8)],
	)

	config.validate()


def test_runner_config_defaults_to_three_concurrent_tasks(tmp_path: Path):
	config = _config(tmp_path)

	assert config.max_concurrency == 3
	config.validate()


@pytest.mark.parametrize(
	('value', 'error'),
	[
		('contact@example.org', 'organization name'),
		('Example Organization no-contact', 'contact email'),
		('Example Organization contact@example.org\r\nX-Injected: true', 'single printable line'),
		('组织 contact@example.org', 'ASCII'),
	],
)
def test_runner_config_rejects_invalid_declared_sec_user_agent(tmp_path: Path, value: str, error: str):
	config = _config(tmp_path, sec_user_agent=value)

	with pytest.raises(ValueError, match=error):
		config.validate()


@pytest.mark.asyncio
async def test_sec_task_without_declared_user_agent_logs_warning(monkeypatch, tmp_path: Path, caplog):
	class FakeRuntime:
		declared_user_agents: list[str | None] = []

		def __init__(
			self,
			_context: Any,
			_task_dir: Path,
			_logger: Any,
			*,
			declared_user_agent: str | None = None,
			task_identity: dict[str, Any] | None = None,
		) -> None:
			self.declared_user_agents.append(declared_user_agent)
			assert task_identity is not None
			self.visited_urls = ['https://www.sec.gov/']

		async def start(self, _website: str) -> None:
			return None

		async def close(self, *, timeout_seconds: float = 60.0) -> None:
			return None

		def capture_payload(self) -> dict[str, Any]:
			return {'capture_time': '2026-07-21 00:00:00', 'total_requests': 0, 'all_requests': []}

	class FakeAgent:
		partial_outcome = None

		def __init__(self, **_kwargs: Any) -> None:
			return None

		async def run(self) -> AgentRunOutcome:
			return AgentRunOutcome(status='SUCCESS', agent_answer='done', evidence=['SEC page'])

	monkeypatch.setattr('browser_use.webretriever.runner.BrowserRuntime', FakeRuntime)
	monkeypatch.setattr('browser_use.webretriever.runner.ProtocolIIIAgent', FakeAgent)
	task = CompetitionTask.model_validate(
		{
			'task_idx': 6,
			'task_id': '6d2ecefa7ec049919234b2e2492a87a1',
			'website': 'https://www.sec.gov/',
			'task': 'Read the filing.',
		}
	)
	logger = __import__('logging').getLogger('test-webretriever-sec-warning')

	with caplog.at_level('WARNING'):
		status = await _run_task(
			context=cast(Any, object()),
			task=task,
			config=_config(tmp_path),
			llm=cast(Any, object()),
			logger=logger,
		)

	assert status == 'SUCCESS'
	assert FakeRuntime.declared_user_agents == [None]
	assert 'running without a declared User-Agent' in caplog.text
	timing = json.loads((tmp_path / 'output' / task.directory_name / 'model_call_timing.json').read_text(encoding='utf-8'))
	assert timing['summary']['attempt_count'] == 0
	assert timing['steps'] == []


@pytest.mark.asyncio
async def test_three_workers_process_six_tasks_concurrently(monkeypatch, tmp_path: Path):
	tasks = [
		CompetitionTask.model_validate(
			{
				'task_idx': index,
				'task_id': f'{index:032x}',
				'website': 'https://example.com',
				'task': f'Read result {index}.',
			}
		)
		for index in range(6)
	]
	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	for task in tasks:
		queue.put_nowait(task)

	active = 0
	peak_active = 0

	async def fake_run_task(**kwargs: Any) -> str:
		nonlocal active, peak_active
		active += 1
		peak_active = max(peak_active, active)
		await asyncio.sleep(0.01)
		active -= 1
		return 'SUCCESS'

	monkeypatch.setattr('browser_use.webretriever.runner._run_task', fake_run_task)
	statuses: dict[str, str] = {}
	config = _config(tmp_path)
	await asyncio.gather(
		*(
			_consume_tasks(
				worker_id=worker_id,
				context=cast(Any, object()),
				queue=queue,
				config=config,
				llm=cast(Any, object()),
				statuses=statuses,
				sec_task_semaphore=asyncio.Semaphore(1),
			)
			for worker_id in range(config.max_concurrency)
		)
	)

	assert peak_active == 3
	assert len(statuses) == 6
	assert set(statuses.values()) == {'SUCCESS'}


@pytest.mark.asyncio
async def test_sec_tasks_are_serialized_while_other_tasks_remain_concurrent(monkeypatch, tmp_path: Path):
	tasks = [
		CompetitionTask.model_validate(
			{
				'task_idx': index,
				'task_id': f'{index:032x}',
				'website': website,
				'task': f'Read result {index}.',
			}
		)
		for index, website in enumerate(('https://www.sec.gov/', 'https://data.sec.gov/', 'https://example.com/'))
	]
	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	for task in tasks:
		queue.put_nowait(task)

	active_sec = 0
	peak_sec = 0
	non_sec_ran_with_sec = False

	async def fake_run_task(**kwargs: Any) -> str:
		nonlocal active_sec, peak_sec, non_sec_ran_with_sec
		is_sec = 'sec.gov' in kwargs['task'].website
		if is_sec:
			active_sec += 1
			peak_sec = max(peak_sec, active_sec)
		else:
			non_sec_ran_with_sec = active_sec > 0
		await asyncio.sleep(0.02)
		if is_sec:
			active_sec -= 1
		return 'SUCCESS'

	monkeypatch.setattr('browser_use.webretriever.runner._run_task', fake_run_task)
	statuses: dict[str, str] = {}
	config = _config(tmp_path)
	sec_task_semaphore = asyncio.Semaphore(1)
	await asyncio.gather(
		*(
			_consume_tasks(
				worker_id=worker_id,
				context=cast(Any, object()),
				queue=queue,
				config=config,
				llm=cast(Any, object()),
				statuses=statuses,
				sec_task_semaphore=sec_task_semaphore,
			)
			for worker_id in range(3)
		)
	)

	assert peak_sec == 1
	assert non_sec_ran_with_sec
	assert set(statuses.values()) == {'SUCCESS'}


def test_runner_config_defaults_to_ten_minute_task_timeout(tmp_path: Path):
	config = _config(tmp_path)

	assert config.task_timeout_seconds == 600
	config.validate()


def test_result_payload_records_end_to_end_task_timing():
	task = CompetitionTask.model_validate(
		{
			'task_idx': 0,
			'task_id': '0123456789abcdef0123456789abcdef',
			'website': 'https://example.com',
			'task': 'Read the result.',
		}
	)
	timing_summary = {
		'decision_step_count': 2,
		'attempt_count': 3,
		'successful_attempt_count': 2,
		'failed_attempt_count': 1,
		'timed_out_attempt_count': 0,
		'cancelled_attempt_count': 0,
		'model_wait_seconds': 12.5,
		'retry_wait_seconds': 8.0,
		'total_wait_seconds': 20.5,
	}
	payload = _result_payload(
		task,
		AgentRunOutcome(status='SUCCESS', duration_seconds=1.25, model_call_timing_summary=timing_summary),
		urls=['https://example.com/result'],
		model='gpt-5.4',
		task_started_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
		task_elapsed_seconds=12.34567,
		task_timeout_seconds=300,
	)

	assert payload['duration_seconds'] == 1.25
	assert payload['task_started_at'] == '2026-07-20T00:00:00+00:00'
	assert payload['task_elapsed_seconds'] == 12.346
	assert payload['task_timeout_seconds'] == 300
	assert payload['thought_language'] == DEFAULT_THOUGHT_LANGUAGE
	assert payload['model_call_timing_summary'] == timing_summary
	assert isinstance(payload['task_completed_at'], str)


def test_result_payload_records_cleanup_without_overriding_the_task_status():
	task = CompetitionTask.model_validate(
		{
			'task_idx': 0,
			'task_id': '0123456789abcdef0123456789abcdef',
			'website': 'https://example.com',
			'task': 'Read the result.',
		}
	)
	payload = _result_payload(
		task,
		AgentRunOutcome(status='FAIL_TASK_TIMEOUT'),
		urls=[],
		model='gpt-5.4',
		cleanup={'status': 'timed_out', 'grace_seconds': 60.0, 'residual_tasks': {'background': 1}},
	)

	assert payload['status'] == 'FAIL_TASK_TIMEOUT'
	assert payload['cleanup'] == {
		'status': 'timed_out',
		'grace_seconds': 60.0,
		'residual_tasks': {'background': 1},
	}


@pytest.mark.asyncio
async def test_task_timeout_persists_result_when_runtime_cleanup_resists_cancellation(monkeypatch, tmp_path: Path):
	cleanup_started = asyncio.Event()
	cleanup_cancelled = asyncio.Event()
	release_cleanup = asyncio.Event()

	class FakeRuntime:
		def __init__(self, *_args: Any, **_kwargs: Any) -> None:
			self.visited_urls = ['https://example.test/']

		async def start(self, _website: str) -> None:
			return None

		async def close(self, *, timeout_seconds: float) -> dict[str, Any]:
			cleanup_started.set()
			try:
				await release_cleanup.wait()
			except asyncio.CancelledError:
				cleanup_cancelled.set()
				await release_cleanup.wait()
			return {'status': 'completed', 'residual_tasks': {}}

		def cleanup_diagnostics(self) -> dict[str, Any]:
			return {'status': 'timed_out', 'residual_tasks': {'background': 1}}

		def capture_payload(self) -> dict[str, Any]:
			return {'capture_time': 'probe', 'total_requests': 0, 'all_requests': []}

	class FakeAgent:
		partial_outcome = None
		model_call_timing_payload = None

		def __init__(self, **_kwargs: Any) -> None:
			return None

		async def run(self) -> AgentRunOutcome:
			await asyncio.sleep(10)
			return AgentRunOutcome(status='SUCCESS')

		def salvage_partial_answer(self) -> None:
			return None

	monkeypatch.setattr('browser_use.webretriever.runner.BrowserRuntime', FakeRuntime)
	monkeypatch.setattr('browser_use.webretriever.runner.ProtocolIIIAgent', FakeAgent)
	monkeypatch.setattr('browser_use.webretriever.runner.TASK_FINALIZATION_GRACE_SECONDS', 0.02)
	loop = asyncio.get_running_loop()
	loop.call_later(0.12, release_cleanup.set)
	task = CompetitionTask.model_validate(
		{
			'task_idx': 0,
			'task_id': 'timeout-cleanup-probe-task-000001',
			'website': 'https://example.test/',
			'task': 'Verify timeout persistence.',
		}
	)
	started_at = loop.time()
	status = await _run_task(
		context=cast(Any, object()),
		task=task,
		config=_config(tmp_path, task_timeout_seconds=0.02),
		llm=cast(Any, object()),
		logger=__import__('logging').getLogger('test-webretriever-timeout-cleanup'),
	)
	elapsed = loop.time() - started_at

	result = json.loads((tmp_path / 'output' / task.directory_name / 'result.json').read_text(encoding='utf-8'))
	await asyncio.sleep(0)
	assert status == 'FAIL_TASK_TIMEOUT'
	assert elapsed < 0.1
	assert cleanup_started.is_set()
	assert cleanup_cancelled.is_set()
	assert result['status'] == 'FAIL_TASK_TIMEOUT'
	assert result['cleanup']['status'] == 'timed_out'
	assert result['cleanup']['residual_tasks'] == {'background': 1}
	writer = TaskArtifactWriter(tmp_path / 'output', task)
	assert writer.acquire_lock(blocking=False)
	writer.release_lock()


def test_missing_experiment_result_is_not_treated_as_zero_verification_episodes(tmp_path: Path):
	task = CompetitionTask.model_validate(
		{
			'task_idx': 55,
			'task_id': 'task-55',
			'website': 'https://example.com',
			'task': 'Read the result.',
		}
	)

	record = _experiment_records_from_artifacts(
		output_dir=tmp_path,
		tasks=[task],
		driver=BrowserDriver.PATCHRIGHT,
		endpoint_label='cdp-0',
		repeat_index=0,
	)[0]

	assert record.challenge_episodes == 0
	assert record.artifact_complete is False


def test_task_timeout_outcome_preserves_partial_agent_history(tmp_path: Path):
	agent = ProtocolIIIAgent(
		task=CompetitionTask.model_validate(
			{
				'task_idx': 0,
				'task_id': '0123456789abcdef0123456789abcdef',
				'website': 'https://example.com',
				'task': 'Read the result.',
			}
		),
		llm=cast(Any, object()),
		runtime=object(),
		task_dir=tmp_path,
	)
	partial = AgentRunOutcome(
		status='FAIL',
		actions=['{"action":"click"}'],
		thoughts=['Clicked the source.'],
		steps=[{'step': 0}],
	)
	agent._partial_outcome = partial

	outcome = _task_timeout_outcome(agent, 300)

	assert outcome is partial
	assert outcome.status == 'FAIL_TASK_TIMEOUT'
	assert outcome.actions == ['{"action":"click"}']
	assert outcome.thoughts == ['Clicked the source.']
	assert outcome.steps == [{'step': 0}]


@pytest.mark.parametrize(
	('override', 'message'),
	[
		({'max_steps': 101}, '100'),
		({'model_timeout_seconds': 181}, '180'),
		({'task_timeout_seconds': 0}, 'greater than 0'),
		({'max_concurrency': 9}, '8'),
		({'cdp_urls': [f'ws://browser.example.test/{index}' for index in range(9)]}, '8 concurrent CDP'),
	],
)
def test_runner_config_rejects_values_above_competition_limits(tmp_path: Path, override: dict[str, object], message: str):
	with pytest.raises(ValueError, match=message):
		_config(tmp_path, **override).validate()


def test_runner_config_allows_task_timeout_above_the_previous_hard_cap(tmp_path: Path):
	_config(tmp_path, task_timeout_seconds=2000).validate()


def test_runner_config_forbids_rerunning_failed_tasks_in_formal_cdp_mode(tmp_path: Path):
	with pytest.raises(ValueError, match='formal CDP runs must not retry'):
		_config(tmp_path, rerun_failed=True).validate()

	_config(tmp_path, local_browser=True, cdp_urls=[], rerun_failed=True).validate()


@pytest.mark.parametrize(
	'model',
	[
		'gpt-5.4',
		'claude-sonnet-4-6',
		'gemini-3.1-pro',
		'grok-4.3',
		'glm-5v-turbo',
		'kimi-k2.6',
	],
)
def test_model_policy_accepts_published_maximum_versions(model: str):
	validate_model_policy(model)


@pytest.mark.parametrize(
	'model',
	[
		'gpt-5.5',
		'claude-sonnet-4-7',
		'gemini-3.2-pro',
		'grok-4.4',
		'glm-6v-turbo',
		'kimi-k2.7',
	],
)
def test_model_policy_rejects_versions_above_published_caps(model: str):
	with pytest.raises(ValueError, match='above the challenge maximum'):
		validate_model_policy(model)


def test_task_timeout_outcome_salvages_verified_memory_into_the_answer(tmp_path: Path):
	agent = ProtocolIIIAgent(
		task=CompetitionTask.model_validate(
			{
				'task_idx': 0,
				'task_id': '0123456789abcdef0123456789abcdef',
				'website': 'https://example.com',
				'task': 'Read the result.',
			}
		),
		llm=cast(Any, object()),
		runtime=object(),
		task_dir=tmp_path,
	)
	agent._partial_outcome = AgentRunOutcome(status='FAIL', steps=[{'step': 0, 'url': 'https://example.com/report'}])
	agent._last_memory = 'Constraints: exact month.\nVerified: 2022/08 price change is -13.7 percent.\nNext: confirm 2024 values.'

	outcome = _task_timeout_outcome(agent, 900)

	assert outcome.status == 'FAIL_TASK_TIMEOUT'
	assert '-13.7' in outcome.agent_answer
	assert outcome.evidence and 'https://example.com/report' in outcome.evidence[0]
	assert 'exact month' not in outcome.agent_answer
	assert 'confirm 2024' not in outcome.agent_answer


def test_task_timeout_outcome_keeps_an_existing_answer_untouched(tmp_path: Path):
	agent = ProtocolIIIAgent(
		task=CompetitionTask.model_validate(
			{
				'task_idx': 0,
				'task_id': '0123456789abcdef0123456789abcdef',
				'website': 'https://example.com',
				'task': 'Read the result.',
			}
		),
		llm=cast(Any, object()),
		runtime=object(),
		task_dir=tmp_path,
	)
	agent._partial_outcome = AgentRunOutcome(status='FAIL', agent_answer='42', evidence=['The page states 42.'])
	agent._last_memory = 'Verified: something else entirely.'

	outcome = _task_timeout_outcome(agent, 900)

	assert outcome.agent_answer == '42'
	assert outcome.evidence == ['The page states 42.']
