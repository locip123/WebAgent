from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from browser_use.webretriever import cli
from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.runner import RunnerConfig, _result_payload, _task_timeout_outcome, build_llm, validate_model_policy


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
	assert config.thought_language == '中文'


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


def test_runner_config_defaults_to_five_minute_task_timeout(tmp_path: Path):
	config = _config(tmp_path)

	assert config.task_timeout_seconds == 300
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
	payload = _result_payload(
		task,
		AgentRunOutcome(status='SUCCESS', duration_seconds=1.25),
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
	assert payload['thought_language'] == '中文'
	assert isinstance(payload['task_completed_at'], str)


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
