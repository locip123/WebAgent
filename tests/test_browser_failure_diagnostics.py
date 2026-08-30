from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserRuntime
from browser_use.webretriever.browser_failures import BrowserFailurePhase, classify_browser_failure
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.runner import RunnerConfig, _run_task


@pytest.mark.parametrize(
	('error', 'phase', 'session_closed', 'status'),
	[
		('Call BrowserRuntime.start(website) first', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_RUNTIME_NOT_STARTED'),
		('BrowserRuntime is closed', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_RUNTIME_CLOSED'),
		('BrowserRuntime has no active task page', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE'),
		('BrowserRuntime has no live safe page', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_NO_LIVE_PAGE'),
		("Download placeholder page ':' has no live safe opener or fallback page", BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_DOWNLOAD_PAGE_ORPHANED'),
		('Target page, context or browser has been closed', BrowserFailurePhase.ACTION, True, 'FAIL_BROWSER_SESSION_LOST'),
		('unexpected Playwright error', BrowserFailurePhase.OBSERVATION, False, 'FAIL_BROWSER_OBSERVATION'),
		('worker has no browser context', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_CONTEXT_UNAVAILABLE'),
		('No CDP worker was available for this task', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_CONNECTION_UNAVAILABLE'),
		('initial navigation failed', BrowserFailurePhase.STARTUP, False, 'FAIL_BROWSER_STARTUP'),
	],
)
def test_browser_failure_statuses_are_stable(
	error: str,
	phase: BrowserFailurePhase,
	session_closed: bool,
	status: str,
) -> None:
	failure = classify_browser_failure(error, phase=phase, session_closed=session_closed)

	assert failure.status == status
	payload = failure.payload(recovery_attempted=session_closed)
	assert payload['category'] == status.removeprefix('FAIL_BROWSER_').lower()
	assert payload['phase'] == phase.value
	assert payload['recovery_attempted'] is session_closed


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
		'phase': 'observation',
		'exception_type': 'RuntimeError',
		'recovery_attempted': False,
	}
	assert outcome.error == 'Observation failed: RuntimeError: Call BrowserRuntime.start(website) first'


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
		'phase': 'startup',
		'exception_type': 'RuntimeError',
		'recovery_attempted': False,
	}
