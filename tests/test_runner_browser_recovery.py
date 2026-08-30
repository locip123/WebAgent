from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright
from playwright._impl._errors import TargetClosedError

from browser_use.webretriever.agent import AgentRunOutcome
from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime
from browser_use.webretriever.models import AgentDecisionEnvelope, InitialPageAgentDecisionEnvelope, WebRetrieverActionResult
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.runner import RunnerConfig, _is_browser_disconnect_error, _run_task, run
from browser_use.webretriever.verification import VerificationAction, VerificationDecision, VerificationState


def test_browser_disconnect_detection_is_narrow() -> None:
	assert _is_browser_disconnect_error('Observation failed: TargetClosedError: browser has been closed')
	assert _is_browser_disconnect_error('Target page, context or browser has been closed')
	assert not _is_browser_disconnect_error('page has been closed by the requested close_tab action')
	assert not _is_browser_disconnect_error('ordinary navigation failed with a 502 response')


def test_disconnect_during_runtime_cleanup_marks_cdp_worker_for_recovery(

	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id='cleanup-disconnect',
		website='http://example.test/',
		task='probe',
	)
	config = RunnerConfig(
		input_path=tmp_path / 'tasks.json',
		output_dir=tmp_path / 'output',
		model='test-model',
		cdp_urls=['http://127.0.0.1:9222'],
		model_services=[ModelServiceConfig('test-service', 'http://127.0.0.1:8000/v1', 'test-key')],
		task_timeout_seconds=5.0,
	)

	class FakeRuntime:
		visited_urls: list[str] = []

		def capture_payload(self) -> dict[str, object]:
			return {'capture_time': 'now', 'total_requests': 0, 'all_requests': []}

		async def close(self, *, timeout_seconds: float) -> dict[str, object]:
			raise TargetClosedError('Target page, context or browser has been closed')

	class FakeBrowserSession:
		abandoned = False

		async def open_task_runtime(self, request: object, *, deadline_monotonic: float) -> FakeRuntime:
			return FakeRuntime()

		async def abandon_interrupted_task(self) -> None:
			self.abandoned = True

	class FakeAgent:
		model_call_timing_payload = None

		def __init__(self, **kwargs: object) -> None:
			pass

		async def run(self) -> AgentRunOutcome:
			return AgentRunOutcome(status='SUCCESS', agent_answer='done', evidence=['observed'])

	monkeypatch.setattr('browser_use.webretriever.runner.ProtocolIIIAgent', FakeAgent)
	session = FakeBrowserSession()
	result = asyncio.run(
		_run_task(
			context=None,
			task=task,
			config=config,
			llm=object(),
			logger=logging.getLogger('test.cleanup-disconnect'),
			browser_session=session,
		)
	)
	assert result.status == 'SUCCESS'
	assert not result.retire_worker
	assert result.recover_worker is True
	assert session.abandoned is True


def _recovery_observation() -> BrowserObservation:
	return BrowserObservation(
		screenshot=b'',
		url='https://example.test/start',
		title='Recovery test page',
		tabs=[{'index': 0, 'url': 'https://example.test/start', 'title': 'Recovery test page', 'active': True}],
		viewport_width=1280,
		viewport_height=720,
		elements=[],
		page_text='A task-owned page is available.',
		recent_network=[],
		downloads=[],
	)


def _recovery_first_click() -> ChatInvokeCompletion[Any]:
	return ChatInvokeCompletion(
		completion=InitialPageAgentDecisionEnvelope.model_validate(
			{
				'decision': {
					'action': 'click',
					'thought': 'Use the visible task-owned page.',
					'current_path_id': '1->1',
					'decision_summary': 'A visible route can be explored.',
					'path_json_action': {
						'operations': [
							{
								'op': 'add',
								'parent_path_id': '1',
								'location': 'visible task entry',
								'strategy_description': 'Open the visible task entry.',
							}
						]
					},
					'element_id': 0,
				}
			}
		),
		raw_completion='{"decision":{"action":"click"}}',
		usage=None,
	)


def _recovery_successful_finish() -> ChatInvokeCompletion[Any]:
	return ChatInvokeCompletion(
		completion=AgentDecisionEnvelope.model_validate(
			{
				'decision': {
					'action': 'finish',
					'thought': 'The recovered page contains the answer.',
					'success': True,
					'answer': 'Recovered answer',
					'evidence': ['The recovered task-owned page.'],
				}
			}
		),
		raw_completion='{"decision":{"action":"finish"}}',
		usage=None,
	)


def _recovery_initial_successful_finish() -> ChatInvokeCompletion[Any]:
	return ChatInvokeCompletion(
		completion=InitialPageAgentDecisionEnvelope.model_validate(
			{
				'decision': {
					'action': 'finish',
					'thought': 'The restored initial page contains the answer.',
					'success': True,
					'answer': 'Restored answer',
					'evidence': ['The restored task-owned page.'],
				}
			}
		),
		raw_completion='{"decision":{"action":"finish"}}',
		usage=None,
	)


def _recovery_click() -> ChatInvokeCompletion[Any]:
	return ChatInvokeCompletion(
		completion=AgentDecisionEnvelope.model_validate(
			{
				'decision': {
					'action': 'click',
					'thought': 'Re-observe after the transient browser error.',
					'current_path_id': '1->1',
					'decision_summary': 'Retry the visible task route after a fresh observation.',
					'path_json_action': {'operations': []},
					'element_id': 0,
				}
			}
		),
		raw_completion='{"decision":{"action":"click"}}',
		usage=None,
	)


class _RecoveryModel:
	def __init__(self, outcomes: list[ChatInvokeCompletion[Any]]) -> None:
		self._outcomes = iter(outcomes)
		self.calls = 0

	async def ainvoke(self, *_args: Any, **_kwargs: Any) -> ChatInvokeCompletion[Any]:
		self.calls += 1
		return next(self._outcomes)


class _ClosedTargetRuntime:
	def __init__(self, *, survives: bool) -> None:
		self.survives = survives
		self.observed_steps: list[int] = []
		self.recovery_calls = 0

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		return _recovery_observation()

	async def execute(self, decision: object) -> WebRetrieverActionResult:
		return WebRetrieverActionResult(
			action='click',
			status='error',
			executed=False,
			state_changed=False,
			error_type='BrowserSessionClosed',
			error='Target page, context or browser has been closed',
			recovery='re_ground',
		)

	async def recover_live_task_page(self) -> bool:
		self.recovery_calls += 1
		return self.survives


def _recovery_agent(runtime: object, model: _RecoveryModel, task_dir: Path) -> ProtocolIIIAgent:
	return ProtocolIIIAgent(
		task=CompetitionTask(
			task_idx=0,
			task_id='browser-session-recovery',
			website='https://example.test/start',
			task='Answer from the recovered page.',
		),
		llm=model,  # type: ignore[arg-type]
		runtime=runtime,
		task_dir=task_dir,
		max_steps=1,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
	)


def test_surviving_task_page_refunds_the_closed_target_step(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _ClosedTargetRuntime, _RecoveryModel]:
		runtime = _ClosedTargetRuntime(survives=True)
		model = _RecoveryModel([_recovery_first_click(), _recovery_successful_finish()])
		outcome = await _recovery_agent(runtime, model, tmp_path).run()
		return outcome, runtime, model

	outcome, runtime, model = asyncio.run(scenario())

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 0]
	assert runtime.recovery_calls == 1
	assert model.calls == 2
	assert [step['action']['action'] for step in outcome.steps] == ['finish']


def test_no_surviving_task_page_fails_browser_without_action_streak(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _ClosedTargetRuntime, _RecoveryModel]:
		runtime = _ClosedTargetRuntime(survives=False)
		model = _RecoveryModel([_recovery_first_click()])
		outcome = await _recovery_agent(runtime, model, tmp_path).run()
		return outcome, runtime, model

	outcome, runtime, model = asyncio.run(scenario())

	assert outcome.status == 'FAIL_BROWSER'
	assert 'no surviving task pages' in (outcome.error or '')
	assert runtime.recovery_calls == 1
	assert model.calls == 1


class _RestorableObservationRuntime:
	def __init__(self) -> None:
		self.restored = False
		self.restore_calls = 0
		self.observed_steps: list[int] = []

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		if not self.restored:
			raise RuntimeError('Call BrowserRuntime.start(website) first')
		return _recovery_observation()

	async def recover_live_task_page(self) -> bool:
		return False

	async def restore_task_page(self) -> bool:
		self.restore_calls += 1
		self.restored = True
		return True


def test_lost_active_page_is_restored_without_terminal_browser_failure(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _RestorableObservationRuntime, _RecoveryModel]:
		runtime = _RestorableObservationRuntime()
		model = _RecoveryModel([_recovery_initial_successful_finish()])
		agent = ProtocolIIIAgent(
			task=CompetitionTask(
				task_idx=0,
				task_id='restore-lost-page',
				website='https://example.test/start',
				task='Answer from a restored task page.',
			),
			llm=model,  # type: ignore[arg-type]
			runtime=runtime,
			task_dir=tmp_path,
			max_steps=1,
			model_timeout_seconds=1.0,
			chart_network_inspector=object(),
		)
		return await agent.run(), runtime, model

	outcome, runtime, model = asyncio.run(scenario())

	assert outcome.status == 'SUCCESS'
	assert runtime.restore_calls == 1
	assert runtime.observed_steps == [0, 0]
	assert model.calls == 1


class _StaleElementRuntime:
	def __init__(self) -> None:
		self.observed_steps: list[int] = []
		self.execute_calls = 0

	async def observe(self, step: int) -> BrowserObservation:
		self.observed_steps.append(step)
		return _recovery_observation()

	async def execute(self, decision: object) -> WebRetrieverActionResult:
		self.execute_calls += 1
		return WebRetrieverActionResult(
			action='click',
			status='error',
			executed=False,
			state_changed=False,
			error_type='StaleElement',
			error='Unknown element index 64; call observe() before interacting',
			recovery='observe',
		)


def test_stale_element_reobservations_do_not_terminally_fail_the_task(tmp_path: Path) -> None:
	async def scenario() -> tuple[object, _StaleElementRuntime, _RecoveryModel]:
		runtime = _StaleElementRuntime()
		model = _RecoveryModel(
			[_recovery_first_click(), *[_recovery_click() for _ in range(4)], _recovery_successful_finish()]
		)
		agent = ProtocolIIIAgent(
			task=CompetitionTask(
				task_idx=0,
				task_id='stale-element-recovery',
				website='https://example.test/start',
				task='Answer after stale-element recovery.',
			),
			llm=model,  # type: ignore[arg-type]
			runtime=runtime,
			task_dir=tmp_path,
			max_steps=6,
			model_timeout_seconds=1.0,
			chart_network_inspector=object(),
		)
		return await agent.run(), runtime, model

	outcome, runtime, model = asyncio.run(scenario())

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 1, 2, 3, 4, 5]
	assert runtime.execute_calls == 5
	assert model.calls == 6


class _BlockedThenClearVerificationController:
	def __init__(self, **_kwargs: object) -> None:
		self.calls = 0

	def decide(self, _observation: BrowserObservation) -> VerificationDecision:
		self.calls += 1
		if self.calls == 1:
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason='bounded verification handling was exhausted',
			)
		return VerificationDecision(
			state=VerificationState.NONE,
			action=VerificationAction.NONE,
			reason='verification no longer blocks the page',
		)

	def summary(self) -> dict[str, object]:
		return {'state': 'none'}


class _FinishOnlyRuntime:
	async def observe(self, _step: int) -> BrowserObservation:
		return _recovery_observation()


def test_verification_budget_exhaustion_returns_control_to_the_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(
		'browser_use.webretriever.agent.VerificationController', _BlockedThenClearVerificationController
	)

	agent = ProtocolIIIAgent(
		task=CompetitionTask(
			task_idx=0,
			task_id='verification-recovery',
			website='https://example.test/start',
			task='Answer after verification recovery.',
		),
		llm=_RecoveryModel([_recovery_initial_successful_finish()]),  # type: ignore[arg-type]
		runtime=_FinishOnlyRuntime(),
		task_dir=tmp_path,
		max_steps=2,
		model_timeout_seconds=1.0,
		chart_network_inspector=object(),
	)

	outcome = asyncio.run(agent.run())

	assert outcome.status == 'SUCCESS'
	assert outcome.steps[0]['action']['action'] == 'verification_blocked'


class _TaskPage:
	def __init__(self, url: str, *, closed: bool = False) -> None:
		self.url = url
		self.closed = closed
		self.brought_to_front = False

	def is_closed(self) -> bool:
		return self.closed

	async def bring_to_front(self) -> None:
		self.brought_to_front = True


def test_runtime_recovery_uses_only_surviving_owned_pages(tmp_path: Path) -> None:
	runtime = BrowserRuntime(context=object(), task_dir=tmp_path, logger=logging.getLogger('runtime-recovery-test'))
	closed_page = _TaskPage('https://example.test/closed', closed=True)
	live_page = _TaskPage('https://example.test/live')
	runtime._started = True
	runtime.page = closed_page  # type: ignore[assignment]
	runtime._owned_pages = [closed_page, live_page]  # type: ignore[assignment]

	assert asyncio.run(runtime.recover_live_task_page()) is True
	assert runtime.page is live_page
	assert live_page.brought_to_front is True


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
