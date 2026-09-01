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
from browser_use.webretriever.artifacts import TaskArtifactWriter
from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime
from browser_use.webretriever.models import AgentDecisionEnvelope, InitialPageAgentDecisionEnvelope, WebRetrieverActionResult
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.browser_session import BrowserRecoveryDeadlineExceeded
from browser_use.webretriever.runner import (
	RunnerConfig,
	TaskRunResult,
	_WorkerPoolState,
	_consume_tasks,
	_is_browser_disconnect_error,
	_run_task,
	run,
)


def test_browser_disconnect_detection_is_narrow() -> None:
	assert _is_browser_disconnect_error('Observation failed: TargetClosedError: browser has been closed')
	assert _is_browser_disconnect_error('Target page, context or browser has been closed')
	assert _is_browser_disconnect_error('Page.screenshot: Target crashed')
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


def test_task_page_unavailable_marks_cdp_worker_for_recovery(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id='missing-task-page-worker-recovery',
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
			return {'status': 'completed'}

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
			return AgentRunOutcome(
				status='FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
				error='Observation failed: RuntimeError: BrowserRuntime has no active task page',
				browser_failure={
					'category': 'task_page_unavailable',
					'subtype': 'task_page_recovery_exhausted',
					'phase': 'observation',
					'exception_type': 'RuntimeError',
					'recovery_attempted': True,
				},
			)

	monkeypatch.setattr('browser_use.webretriever.runner.ProtocolIIIAgent', FakeAgent)
	session = FakeBrowserSession()
	result = asyncio.run(
		_run_task(
			context=None,
			task=task,
			config=config,
			llm=object(),
			logger=logging.getLogger('test.missing-task-page-worker-recovery'),
			browser_session=session,  # type: ignore[arg-type]
		)
	)

	assert result.status == 'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE'
	assert result.recover_worker is True
	assert result.retire_worker is False
	assert session.abandoned is True


def test_unstarted_cdp_task_is_requeued_for_another_worker(
	tmp_path: Path,
) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id='unstarted-cdp-handoff',
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

	class FailedStartupSession:
		recovery_required = False

		async def open_task_runtime(self, request: object, *, deadline_monotonic: float) -> object:
			raise BrowserRecoveryDeadlineExceeded('CDP connection unavailable')

	session = FailedStartupSession()
	result = asyncio.run(
		_run_task(
			context=None,
			task=task,
			config=config,
			llm=object(),
			logger=logging.getLogger('test.unstarted-cdp-handoff'),
			browser_session=session,  # type: ignore[arg-type]
		)
	)

	assert result.status == 'REQUEUED'
	assert result.requeue_task is True
	assert result.retire_worker is True
	writer = TaskArtifactWriter(config.output_dir, task)
	assert json.loads(writer.result_path.read_text(encoding='utf-8'))['status'] == 'PENDING'


def test_requeued_unstarted_task_is_consumed_by_a_healthy_worker(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	task = CompetitionTask(task_idx=0, task_id='handoff-queue', website='http://example.test/', task='probe')
	config = RunnerConfig(
		input_path=tmp_path / 'tasks.json',
		output_dir=tmp_path / 'output',
		model='test-model',
		cdp_urls=['http://127.0.0.1:9222'],
		model_services=[ModelServiceConfig('test-service', 'http://127.0.0.1:8000/v1', 'test-key')],
	)
	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	queue.put_nowait(task)
	statuses: dict[str, str] = {}

	class Session:
		recovery_required = False

	async def fake_run_task(**kwargs: object) -> TaskRunResult:
		call_count = fake_run_task.call_count
		fake_run_task.call_count += 1
		if call_count == 0:
			return TaskRunResult('REQUEUED', retire_worker=True, requeue_task=True)
		return TaskRunResult('SUCCESS')
	fake_run_task.call_count = 0  # type: ignore[attr-defined]

	monkeypatch.setattr('browser_use.webretriever.runner._run_task', fake_run_task)

	async def scenario() -> None:
		for worker_id in (0, 1):
			await _consume_tasks(
				worker_id=worker_id,
				context=None,
				queue=queue,
				config=config,
				llm=object(),
				statuses=statuses,
				sec_task_semaphore=asyncio.Semaphore(1),
				browser_session=Session(),  # type: ignore[arg-type]
			)

	asyncio.run(scenario())
	assert statuses == {task.task_id: 'SUCCESS'}
	assert queue.empty()


def test_idle_worker_waits_for_a_late_requeued_task(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	task = CompetitionTask(task_idx=0, task_id='late-handoff', website='http://example.test/', task='probe')
	config = RunnerConfig(
		input_path=tmp_path / 'tasks.json',
		output_dir=tmp_path / 'output',
		model='test-model',
		cdp_urls=['http://127.0.0.1:9222'],
		model_services=[ModelServiceConfig('test-service', 'http://127.0.0.1:8000/v1', 'test-key')],
	)
	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	queue.put_nowait(task)
	statuses: dict[str, str] = {}
	pool = _WorkerPoolState(starting_workers=2)

	async def mark_ready() -> None:
		await pool.startup_finished(succeeded=True)
		await pool.startup_finished(succeeded=True)

	async def fake_run_task(**kwargs: object) -> TaskRunResult:
		if not hasattr(fake_run_task, 'started'):
			fake_run_task.started = asyncio.Event()  # type: ignore[attr-defined]
			fake_run_task.release = asyncio.Event()  # type: ignore[attr-defined]
			fake_run_task.started.set()  # type: ignore[attr-defined]
			await fake_run_task.release.wait()  # type: ignore[attr-defined]
			return TaskRunResult('REQUEUED', retire_worker=True, requeue_task=True)
		return TaskRunResult('SUCCESS')

	monkeypatch.setattr('browser_use.webretriever.runner._run_task', fake_run_task)

	class Session:
		recovery_required = False

	async def scenario() -> None:
		await mark_ready()
		first = asyncio.create_task(
			_consume_tasks(
				worker_id=0,
				context=None,
				queue=queue,
				config=config,
				llm=object(),
				statuses=statuses,
				sec_task_semaphore=asyncio.Semaphore(1),
				browser_session=Session(),  # type: ignore[arg-type]
				worker_pool=pool,
			)
		)
		await asyncio.sleep(0)
		await fake_run_task.started.wait()  # type: ignore[attr-defined]
		second = asyncio.create_task(
			_consume_tasks(
				worker_id=1,
				context=None,
				queue=queue,
				config=config,
				llm=object(),
				statuses=statuses,
				sec_task_semaphore=asyncio.Semaphore(1),
				browser_session=Session(),  # type: ignore[arg-type]
				worker_pool=pool,
			)
		)
		await asyncio.sleep(0)
		assert not second.done()
		fake_run_task.release.set()  # type: ignore[attr-defined]
		await asyncio.gather(first, second)

	asyncio.run(scenario())
	assert statuses == {task.task_id: 'SUCCESS'}


def test_runner_replaces_an_unstarted_runtime_with_a_clean_cdp_runtime(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id='unstarted-runtime-replacement',
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

		def __init__(self) -> None:
			self.close_calls = 0

		def capture_payload(self) -> dict[str, object]:
			return {'capture_time': 'now', 'total_requests': 0, 'all_requests': []}

		async def close(self, *, timeout_seconds: float) -> dict[str, object]:
			self.close_calls += 1
			return {'status': 'completed'}

	class FakeBrowserSession:
		def __init__(self) -> None:
			self.initial_runtime = FakeRuntime()
			self.replacement_runtime = FakeRuntime()
			self.replace_calls = 0

		async def open_task_runtime(self, request: object, *, deadline_monotonic: float) -> FakeRuntime:
			return self.initial_runtime

		async def replace_unstarted_task_runtime(self, request: object, *, deadline_monotonic: float) -> FakeRuntime:
			self.replace_calls += 1
			return self.replacement_runtime

	class FakeAgent:
		model_call_timing_payload = None

		def __init__(self, *, runtime: FakeRuntime, recover_unstarted_runtime, **kwargs: object) -> None:
			self.runtime = runtime
			self._recover_unstarted_runtime = recover_unstarted_runtime

		async def run(self) -> AgentRunOutcome:
			self.runtime = await self._recover_unstarted_runtime()
			return AgentRunOutcome(status='SUCCESS', agent_answer='done', evidence=['recovered'])

	monkeypatch.setattr('browser_use.webretriever.runner.ProtocolIIIAgent', FakeAgent)
	session = FakeBrowserSession()
	result = asyncio.run(
		_run_task(
			context=None,
			task=task,
			config=config,
			llm=object(),
			logger=logging.getLogger('test.unstarted-runtime-replacement'),
			browser_session=session,  # type: ignore[arg-type]
		)
	)

	assert result.status == 'SUCCESS'
	assert session.replace_calls == 1
	assert session.initial_runtime.close_calls == 1
	assert session.replacement_runtime.close_calls == 1


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
					'decision_summary': '已在恢复后的页面确认答案；下一步提交该答案，以完成任务。',
					'success': True,
					'answer': 'Recovered answer',
					'evidence': ['The recovered task-owned page.'],
				}
			}
		),
		raw_completion='{"decision":{"action":"finish"}}',
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

	assert outcome.status == 'FAIL_BROWSER_SESSION_LOST'
	assert outcome.browser_failure == {
		'category': 'session_lost',
		'subtype': 'session_closed',
		'phase': 'action',
		'exception_type': 'BrowserActionError',
		'recovery_attempted': True,
	}
	assert 'no surviving task pages' in (outcome.error or '')
	assert runtime.recovery_calls == 1
	assert model.calls == 1


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
	assert statuses['task-1'] == 'FAIL_BROWSER_CONNECTION_UNAVAILABLE'
	assert statuses['task-2'] == 'FAIL_BROWSER_CONNECTION_UNAVAILABLE'
	assert 'FAIL_RUNTIME' not in statuses.values()
