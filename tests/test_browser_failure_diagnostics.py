from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime
from browser_use.webretriever.browser_failures import BrowserFailurePhase, classify_browser_failure
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.models import CompetitionTask, InitialPageAgentDecisionEnvelope
from browser_use.webretriever.runner import RunnerConfig, _run_task


@pytest.mark.parametrize(
	('error', 'phase', 'session_closed', 'status', 'subtype'),
	[
		('Call BrowserRuntime.start(website) first', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_RUNTIME_NOT_STARTED', 'runtime_not_started'),
		('BrowserRuntime is closed', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_RUNTIME_CLOSED', 'runtime_closed'),
		('BrowserRuntime has no active task page', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE', 'task_page_missing'),
		('BrowserRuntime has no live safe page', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_NO_LIVE_PAGE', 'no_live_page'),
		("Download placeholder page ':' has no live safe opener or fallback page", BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_DOWNLOAD_PAGE_ORPHANED', 'download_page_orphaned'),
		('Target page, context or browser has been closed', BrowserFailurePhase.ACTION, True, 'FAIL_BROWSER_SESSION_LOST', 'session_closed'),
		('unexpected Playwright error', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_OBSERVATION', 'unknown'),
		('worker has no browser context', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_CONTEXT_UNAVAILABLE', 'context_unavailable'),
		('No CDP worker was available for this task', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_CONNECTION_UNAVAILABLE', 'connection_unavailable'),
		('initial navigation failed', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_STARTUP', 'navigation_failed'),
	],
)
def test_browser_failure_statuses_are_stable(
	error: str,
	phase: BrowserFailurePhase,
	session_closed: bool,
	status: str,
	subtype: str,
) -> None:
	failure = classify_browser_failure(error, phase=phase, session_closed=session_closed)

	assert failure.status == status
	payload = failure.payload(recovery_attempted=session_closed)
	assert payload['category'] == status.removeprefix('FAIL_BROWSER_').lower()
	assert payload['subtype'] == subtype
	assert payload['phase'] == phase.value
	assert payload['recovery_attempted'] is session_closed


@pytest.mark.parametrize(
	('error', 'phase', 'kwargs', 'status', 'subtype'),
	[
		(
			'BrowserRuntime has no active task page',
			BrowserFailurePhase.OBSERVATION,
			{'task_page_state': 'closed'},
			'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
			'task_page_closed',
		),
		(
			'BrowserRuntime active task page is closed',
			BrowserFailurePhase.OBSERVATION,
			{},
			'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
			'task_page_closed',
		),
		(
			'BrowserRuntime has no active task page',
			BrowserFailurePhase.OBSERVATION,
			{'recovery_exhausted': True},
			'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
			'task_page_recovery_exhausted',
		),
		(
			'Page.screenshot: Target crashed',
			BrowserFailurePhase.OBSERVATION,
			{},
			'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
			'target_crashed',
		),
		(
			'Playwright screenshot timed out and CDP screenshot recovery exceeded the 20-second hard deadline',
			BrowserFailurePhase.OBSERVATION,
			{},
			'FAIL_BROWSER_OBSERVATION',
			'screenshot_timeout',
		),
		(
			'CDP screenshot recovery exceeded the 20-second hard deadline',
			BrowserFailurePhase.OBSERVATION,
			{},
			'FAIL_BROWSER_OBSERVATION',
			'screenshot_timeout',
		),
		(
			'BrowserType.connect_over_cdp: Timeout 60000ms exceeded',
			BrowserFailurePhase.STARTUP,
			{},
			'FAIL_BROWSER_CONNECTION_UNAVAILABLE',
			'cdp_connection_timeout',
		),
	],
)
def test_browser_failure_subtypes_identify_actionable_causes(
	error: str,
	phase: BrowserFailurePhase,
	kwargs: dict[str, object],
	status: str,
	subtype: str,
) -> None:
	failure = classify_browser_failure(error, phase=phase, **kwargs)

	assert failure.status == status
	assert failure.payload(recovery_attempted=False)['subtype'] == subtype


class _NotStartedRuntime:
	async def observe(self, _step: int) -> object:
		raise RuntimeError('Call BrowserRuntime.start(website) first')


def _task() -> CompetitionTask:
	return CompetitionTask(
		task_idx=28,
		task_id='browser-failure-diagnostic',
		website='https://example.test/',
		task='Inspect the browser failure classification.',
	)


def test_observation_failure_persists_specific_status_and_diagnostic(tmp_path: Path) -> None:
	agent = ProtocolIIIAgent(
		task=_task(),
		llm=object(),  # type: ignore[arg-type]
		runtime=_NotStartedRuntime(),
		task_dir=tmp_path,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'FAIL_BROWSER_RUNTIME_NOT_STARTED'
	assert outcome.browser_failure == {
		'category': 'runtime_not_started',
		'subtype': 'runtime_not_started',
		'phase': 'observation',
		'exception_type': 'RuntimeError',
		'recovery_attempted': False,
	}
	assert outcome.error == 'Observation failed: RuntimeError: Call BrowserRuntime.start(website) first'


def _recovered_observation() -> BrowserObservation:
	return BrowserObservation(
		screenshot=b'',
		url='https://example.test/start',
		title='Recovered task page',
		tabs=[{'index': 0, 'url': 'https://example.test/start', 'title': 'Recovered task page', 'active': True}],
		viewport_width=1280,
		viewport_height=720,
		elements=[],
		page_text='The recovered task page contains the answer.',
		recent_network=[],
		downloads=[],
	)


class _RecoveryModel:
	async def ainvoke(self, *_args: Any, **_kwargs: Any) -> ChatInvokeCompletion[Any]:
		return ChatInvokeCompletion(
			completion=InitialPageAgentDecisionEnvelope.model_validate(
				{
					'decision': {
						'action': 'finish',
						'thought': 'The replacement task page has the answer.',
						'success': True,
						'answer': 'Recovered answer',
						'evidence': ['The replacement task page contains the answer.'],
						'path_json_action': {'operations': []},
					}
				}
			),
			raw_completion='{"decision":{"action":"finish"}}',
			usage=None,
		)


class _DirectRestartFailureRuntime:
	def __init__(self) -> None:
		self.observed_steps: list[int] = []
		self.start_calls = 0

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		raise RuntimeError('Call BrowserRuntime.start(website) first')

	async def start(self, _website: str) -> None:
		self.start_calls += 1
		raise RuntimeError('direct runtime restart failed')


class _ReplacementRuntime:
	def __init__(self) -> None:
		self.observed_steps: list[int] = []

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		return _recovered_observation()


class _DirectRestartSuccessRuntime:
	def __init__(self) -> None:
		self.observed_steps: list[int] = []
		self.start_calls = 0
		self.started = False

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		if not self.started:
			raise RuntimeError('Call BrowserRuntime.start(website) first')
		return _recovered_observation()

	async def start(self, _website: str) -> None:
		self.start_calls += 1
		self.started = True


class _MissingTaskPageRuntime:
	def __init__(self, *, restart_succeeds: bool, observation_after_restart_fails: bool = False) -> None:
		self.restart_succeeds = restart_succeeds
		self.observation_after_restart_fails = observation_after_restart_fails
		self.observed_steps: list[int] = []
		self.restart_calls = 0

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		if self.restart_calls == 0 or (self.observation_after_restart_fails and self.restart_calls == 1):
			raise RuntimeError('BrowserRuntime has no active task page')
		return _recovered_observation()

	async def restart_task_page(self, _website: str, *, timeout_seconds: float) -> None:
		assert timeout_seconds == pytest.approx(60.0)
		self.restart_calls += 1
		if not self.restart_succeeds:
			raise RuntimeError('task page restart failed')


def test_missing_task_page_restarts_in_place_without_consuming_step(tmp_path: Path) -> None:
	runtime = _MissingTaskPageRuntime(restart_succeeds=True)
	agent = ProtocolIIIAgent(
		task=_task(),
		llm=_RecoveryModel(),  # type: ignore[arg-type]
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 0]
	assert runtime.restart_calls == 1
	prompt_payload = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	assert 'task page was rebuilt' in '\n'.join(prompt_payload['steps'][0]['prompt']).lower()


def test_missing_task_page_escalates_and_records_both_recovery_stages(tmp_path: Path) -> None:
	runtime = _MissingTaskPageRuntime(restart_succeeds=False)
	callback_calls = 0

	async def clean_runtime() -> None:
		nonlocal callback_calls
		callback_calls += 1
		return None

	agent = ProtocolIIIAgent(
		task=_task(),
		llm=object(),  # type: ignore[arg-type]
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
		recover_missing_task_page=clean_runtime,
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE'
	assert callback_calls == 1
	assert outcome.browser_failure is not None
	assert outcome.browser_failure['subtype'] == 'task_page_recovery_exhausted'
	assert outcome.browser_failure['recovery_attempted'] is True
	assert [stage['stage'] for stage in outcome.browser_failure['recovery_stages']] == [
		'same_context_page',
		'clean_cdp_worker',
	]


def test_missing_task_page_escalates_after_restart_observation_still_fails(tmp_path: Path) -> None:
	runtime = _MissingTaskPageRuntime(restart_succeeds=True, observation_after_restart_fails=True)
	replacement = _ReplacementRuntime()
	callback_calls = 0

	async def clean_runtime() -> _ReplacementRuntime:
		nonlocal callback_calls
		callback_calls += 1
		return replacement

	agent = ProtocolIIIAgent(
		task=_task(),
		llm=_RecoveryModel(),  # type: ignore[arg-type]
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
		recover_missing_task_page=clean_runtime,
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 0]
	assert callback_calls == 1
	assert replacement.observed_steps == [0]


def test_unstarted_observation_restarts_the_current_runtime_once(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _DirectRestartSuccessRuntime, int]:
		runtime = _DirectRestartSuccessRuntime()
		fallback_calls = 0

		async def unexpected_clean_worker_recovery() -> None:
			nonlocal fallback_calls
			fallback_calls += 1

		agent = ProtocolIIIAgent(
			task=_task(),
			llm=_RecoveryModel(),  # type: ignore[arg-type]
			runtime=runtime,
			task_dir=tmp_path,
			max_steps=1,
			model_timeout_seconds=1.0,
			chart_network_inspector=object(),
			recover_unstarted_runtime=unexpected_clean_worker_recovery,
		)
		return await agent.run(), runtime, fallback_calls

	outcome, runtime, fallback_calls = asyncio.run(scenario())
	prompt_payload = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	prompt_text = '\n'.join(prompt_payload['steps'][0]['prompt'])

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 0]
	assert runtime.start_calls == 1
	assert fallback_calls == 0
	assert 'Browser runtime was restarted' in prompt_text


def test_unstarted_observation_escalates_to_clean_runtime_and_reinforms_model(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _DirectRestartFailureRuntime, _ReplacementRuntime, int]:
		direct_runtime = _DirectRestartFailureRuntime()
		replacement_runtime = _ReplacementRuntime()
		fallback_calls = 0

		async def recover_with_clean_worker() -> _ReplacementRuntime:
			nonlocal fallback_calls
			fallback_calls += 1
			return replacement_runtime

		agent = ProtocolIIIAgent(
			task=_task(),
			llm=_RecoveryModel(),  # type: ignore[arg-type]
			runtime=direct_runtime,
			task_dir=tmp_path,
			max_steps=1,
			model_timeout_seconds=1.0,
			chart_network_inspector=object(),
			recover_unstarted_runtime=recover_with_clean_worker,
		)
		return await agent.run(), direct_runtime, replacement_runtime, fallback_calls

	outcome, direct_runtime, replacement_runtime, fallback_calls = asyncio.run(scenario())
	prompt_payload = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	prompt_text = '\n'.join(prompt_payload['steps'][0]['prompt'])

	assert outcome.status == 'SUCCESS'
	assert direct_runtime.observed_steps == [0]
	assert direct_runtime.start_calls == 1
	assert fallback_calls == 1
	assert replacement_runtime.observed_steps == [0]
	assert 'Browser runtime was restarted' in prompt_text


def test_unstarted_observation_failure_records_the_attempt(tmp_path: Path) -> None:
	agent = ProtocolIIIAgent(
		task=_task(),
		llm=object(),  # type: ignore[arg-type]
		runtime=_DirectRestartFailureRuntime(),
		task_dir=tmp_path,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'FAIL_BROWSER_RUNTIME_NOT_STARTED'
	assert outcome.browser_failure == {
		'category': 'runtime_not_started',
		'subtype': 'runtime_not_started',
		'phase': 'observation',
		'exception_type': 'RuntimeError',
		'recovery_attempted': True,
	}


def test_runtime_distinguishes_not_started_from_missing_task_page(tmp_path: Path) -> None:
	runtime = BrowserRuntime(context=object(), task_dir=tmp_path, logger=logging.getLogger('test.browser-runtime-state'))

	with pytest.raises(RuntimeError, match=r'Call BrowserRuntime\.start\(website\) first'):
		asyncio.run(runtime.observe(0))

	runtime._started = True
	with pytest.raises(RuntimeError, match='BrowserRuntime has no active task page'):
		asyncio.run(runtime.observe(0))


def test_runner_persists_startup_failure_diagnostic(tmp_path: Path) -> None:
	config = RunnerConfig(
		input_path=tmp_path / 'tasks.json',
		output_dir=tmp_path / 'output',
		model='test-model',
		cdp_urls=['http://127.0.0.1:9222'],
		model_services=[ModelServiceConfig('test-service', 'http://127.0.0.1:8000/v1', 'test-key')],
	)
	task = _task()

	result = asyncio.run(
		_run_task(
			context=None,
			task=task,
			config=config,
			llm=object(),  # type: ignore[arg-type]
			logger=logging.getLogger('test.browser-failure-startup'),
		)
	)
	payload: dict[str, Any] = json.loads((config.output_dir / task.directory_name / 'result.json').read_text(encoding='utf-8'))

	assert result.status == 'FAIL_BROWSER_CONTEXT_UNAVAILABLE'
	assert payload['status'] == result.status
	assert payload['browser_failure'] == {
		'category': 'context_unavailable',
		'subtype': 'context_unavailable',
		'phase': 'startup',
		'exception_type': 'RuntimeError',
		'recovery_attempted': False,
	}
