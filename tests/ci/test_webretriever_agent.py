from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from browser_use.llm.exceptions import ModelProviderError
from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.models import AgentDecision, CompetitionTask
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE


def _png_bytes() -> bytes:
	buffer = BytesIO()
	Image.new('RGB', (8, 8), color='white').save(buffer, format='PNG')
	return buffer.getvalue()


@dataclass(slots=True)
class FakeObservation:
	url: str = 'https://example.com/result'
	screenshot: bytes = field(default_factory=_png_bytes)
	elements: list[Any] = field(default_factory=list)

	def render_text(self) -> str:
		return 'Visible page text: independently verified result is 42.'


class FakeRuntime:
	def __init__(self) -> None:
		self.observed_steps: list[int] = []
		self.executed: list[AgentDecision] = []

	async def observe(self, step: int) -> FakeObservation:
		self.observed_steps.append(step)
		return FakeObservation()

	async def execute(self, decision: AgentDecision) -> str:
		self.executed.append(decision)
		return 'Action completed.'


class BlockingExecuteRuntime(FakeRuntime):
	def __init__(self) -> None:
		super().__init__()
		self.action_started = asyncio.Event()

	async def execute(self, decision: AgentDecision) -> str:
		self.executed.append(decision)
		self.action_started.set()
		await asyncio.Event().wait()
		raise AssertionError('unreachable')


class FakeLLM:
	def __init__(self, decisions: list[AgentDecision]) -> None:
		self.decisions = iter(decisions)
		self.calls: list[tuple[list[Any], Any]] = []

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		return SimpleNamespace(completion=next(self.decisions), usage=None)


class FakeChartNetworkInspector:
	def __init__(self, output: str) -> None:
		self.output = output
		self.calls: list[dict[str, Any]] = []

	async def execute(self, **kwargs: Any) -> Any:
		self.calls.append(kwargs)
		return SimpleNamespace(output=self.output, usage={'input_tokens': 11})


class FakeDataAnalysisAssistant:
	def __init__(self, output: str) -> None:
		self.output = output
		self.calls: list[dict[str, Any]] = []

	async def execute(self, **kwargs: Any) -> Any:
		self.calls.append(kwargs)
		return SimpleNamespace(output=self.output, usage={'output_tokens': 7})


class HangingLLM:
	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		await asyncio.Event().wait()
		raise AssertionError('unreachable')


class CancellationResistantLLM:
	def __init__(self) -> None:
		self.release = asyncio.Event()
		self.cancelled = asyncio.Event()

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		try:
			await asyncio.Event().wait()
		except asyncio.CancelledError:
			self.cancelled.set()
			await self.release.wait()
		return SimpleNamespace(completion=None, usage=None)


class TimeoutOnceLLM(FakeLLM):
	def __init__(self, decisions: list[AgentDecision]) -> None:
		super().__init__(decisions)
		self.timeout_returned = False

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		if not self.timeout_returned:
			self.timeout_returned = True
			await asyncio.sleep(60)
		return SimpleNamespace(completion=next(self.decisions), usage=None)


class InvalidOnceLLM(FakeLLM):
	def __init__(self, decisions: list[AgentDecision]) -> None:
		super().__init__(decisions)
		self.invalid_returned = False

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		if not self.invalid_returned:
			self.invalid_returned = True
			raise ModelProviderError('1 validation error for AgentDecision\naction\n  Field required')
		return SimpleNamespace(completion=next(self.decisions), usage=None)


class FirstActionThenHangingLLM:
	def __init__(self) -> None:
		self.calls = 0
		self.second_call_started = asyncio.Event()

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls += 1
		if self.calls == 1:
			return SimpleNamespace(
				completion=AgentDecision(action='wait', seconds=0.1, thought='Record this action first.'),
				usage=None,
			)
		self.second_call_started.set()
		await asyncio.Event().wait()
		raise AssertionError('unreachable')


def _task_with_ground_truth(secret: str = 'SECRET_GROUND_TRUTH_9f6a') -> CompetitionTask:
	return CompetitionTask.model_validate(
		{
			'task_idx': 7,
			'task_id': '0123456789abcdef0123456789abcdef',
			'website': 'https://example.com',
			'task': 'Find the independently verified result.',
			'answer': secret,
		}
	)


def _model_text(calls: list[tuple[list[Any], Any]]) -> str:
	return '\n'.join(message.text for messages, _ in calls for message in messages)


@pytest.mark.asyncio
async def test_agent_success_is_grounded_and_ground_truth_never_enters_prompt(tmp_path: Path):
	secret = 'SECRET_GROUND_TRUTH_9f6a'
	llm = FakeLLM(
		[
			AgentDecision(
				action='finish',
				thought='The visible page contains the answer.',
				memory='Verified on the current page.',
				answer='42',
				evidence=['The result page visibly states 42.'],
				success=True,
			)
		]
	)
	runtime = FakeRuntime()
	task = _task_with_ground_truth(secret)

	outcome = await ProtocolIIIAgent(task=task, llm=llm, runtime=runtime, task_dir=tmp_path).run()

	assert outcome.status == 'SUCCESS'
	assert outcome.agent_answer == '42'
	assert outcome.evidence == ['The result page visibly states 42.']
	assert outcome.actions and 'finish' in outcome.actions[0]
	assert runtime.executed == []
	assert (tmp_path / 'trajectory' / '0.png').is_file()
	assert (tmp_path / 'trajectory_visual' / '0.png').is_file()
	assert secret not in _model_text(llm.calls)
	assert secret not in str(task.model_dump())
	assert llm.calls[0][1] is AgentDecision
	assert f'thought: write in {DEFAULT_THOUGHT_LANGUAGE}.' in _model_text(llm.calls)


@pytest.mark.asyncio
async def test_agent_uses_configured_language_for_thoughts(tmp_path: Path):
	llm = FakeLLM(
		[
			AgentDecision(
				action='finish',
				answer='42',
				evidence=['The current page states 42.'],
				success=True,
			)
		]
	)

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		thought_language='English',
	).run()

	assert outcome.status == 'SUCCESS'
	assert 'thought: write in English.' in _model_text(llm.calls)


@pytest.mark.asyncio
async def test_agent_stops_on_model_timeout_without_browser_action(tmp_path: Path):
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=HangingLLM(),
		runtime=runtime,
		task_dir=tmp_path,
		model_timeout_seconds=0.01,
		max_consecutive_model_timeouts=1,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MODEL_TIMEOUT'
	assert outcome.error is not None and '0.01 seconds' in outcome.error
	assert runtime.executed == []
	assert (tmp_path / 'trajectory_visual' / '0.png').is_file()


@pytest.mark.asyncio
async def test_agent_timeout_is_hard_when_model_resists_cancellation(tmp_path: Path):
	llm = CancellationResistantLLM()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		model_timeout_seconds=0.01,
		max_consecutive_model_timeouts=1,
	)

	started = asyncio.get_running_loop().time()
	outcome = await agent.run()
	elapsed = asyncio.get_running_loop().time() - started

	assert outcome.status == 'FAIL_MODEL_TIMEOUT'
	assert elapsed < 0.5
	await asyncio.wait_for(llm.cancelled.wait(), timeout=0.5)
	llm.release.set()
	await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_agent_retains_partial_outcome_when_cancelled_by_task_watchdog(tmp_path: Path):
	llm = FirstActionThenHangingLLM()
	agent = ProtocolIIIAgent(task=_task_with_ground_truth(), llm=llm, runtime=FakeRuntime(), task_dir=tmp_path)
	run_task = asyncio.create_task(agent.run())

	await asyncio.wait_for(llm.second_call_started.wait(), timeout=0.5)
	run_task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await run_task

	partial = agent.partial_outcome
	assert partial is not None
	assert partial.actions and '"action":"wait"' in partial.actions[0]
	assert partial.thoughts == ['Record this action first.']
	assert partial.steps[0]['action']['action'] == 'wait'


@pytest.mark.asyncio
async def test_agent_retains_in_progress_step_when_browser_action_is_cancelled(tmp_path: Path):
	runtime = BlockingExecuteRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM([AgentDecision(action='wait', seconds=0.1, thought='Wait for the source.')]),
		runtime=runtime,
		task_dir=tmp_path,
	)
	run_task = asyncio.create_task(agent.run())

	await asyncio.wait_for(runtime.action_started.wait(), timeout=0.5)
	run_task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await run_task

	partial = agent.partial_outcome
	assert partial is not None
	assert partial.actions and '"action":"wait"' in partial.actions[0]
	assert partial.thoughts == ['Wait for the source.']
	assert partial.steps == [
		{
			'step': 0,
			'url': 'https://example.com/result',
			'thought': 'Wait for the source.',
			'action': {'action': 'wait', 'seconds': 0.1},
			'outcome': 'Action started; browser result was not recorded yet.',
		}
	]


@pytest.mark.asyncio
async def test_agent_recovers_from_one_model_timeout_within_step_budget(tmp_path: Path):
	llm = TimeoutOnceLLM(
		[
			AgentDecision(
				action='finish',
				answer='42',
				evidence=['The current page states 42.'],
				success=True,
			)
		]
	)
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		model_timeout_seconds=0.01,
	)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 1]
	assert len(llm.calls) == 2
	assert outcome.steps[0]['action'] == {}
	assert 'exceeded 0.01 seconds' in outcome.steps[0]['outcome']


@pytest.mark.asyncio
async def test_agent_recovers_from_invalid_structured_output_within_step_budget(tmp_path: Path):
	llm = InvalidOnceLLM(
		[
			AgentDecision(
				action='finish',
				answer='42',
				evidence=['The current page states 42.'],
				success=True,
			)
		]
	)
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(task=_task_with_ground_truth(), llm=llm, runtime=runtime, task_dir=tmp_path)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert runtime.observed_steps == [0, 1]
	assert len(llm.calls) == 2
	assert len(outcome.actions) == 1
	assert outcome.steps[0]['action'] == {}
	assert 'not a valid AgentDecision' in outcome.steps[0]['outcome']


@pytest.mark.asyncio
async def test_agent_stops_at_configured_max_steps(tmp_path: Path):
	llm = FakeLLM(
		[
			AgentDecision(action='wait', seconds=0.1, thought='Wait once.'),
			AgentDecision(action='wait', seconds=0.1, thought='Wait twice.'),
		]
	)
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=2,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MAX_STEPS'
	assert outcome.error is not None and '2 steps' in outcome.error
	assert runtime.observed_steps == [0, 1]
	assert len(runtime.executed) == 2
	assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_agent_orchestrates_find_then_data_analysis_without_runtime_execute(tmp_path: Path):
	data_dir = str((tmp_path / 'chart_data' / 'scan-1').resolve())
	Path(data_dir).mkdir(parents=True)
	chart_output = json.dumps(
		{
			'action': 'find_chart_data_requests',
			'status': 'ready',
			'artifact_id': 'scan-1',
			'data_dir': data_dir,
			'manifest_sha256': 'a' * 64,
			'datasets': [{'active_filters': {'Year': '2023'}}],
		},
		separators=(',', ':'),
	)
	analysis_output = (
		'{"action":"call_data_analysis_assistant","status":"ok","answer":"November 2023","evidence_rows":[{"month":"2023-11"}]}'
	)
	chart_inspector = FakeChartNetworkInspector(chart_output)
	analysis_assistant = FakeDataAnalysisAssistant(analysis_output)
	query = 'Which 2023 month had the highest Kansai share of all foreign entrants?'
	llm = FakeLLM(
		[
			AgentDecision(action='find_chart_data_requests', thought='Save the filtered chart data.'),
			AgentDecision(
				action='call_data_analysis_assistant',
				analysis_query=query,
				data_dir=data_dir,
				thought='Analyze the normalized tables.',
			),
			AgentDecision(
				action='finish',
				answer='November 2023',
				evidence=['The saved chart table identifies 2023-11 as the maximum ratio.'],
				success=True,
			),
		]
	)
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		chart_network_inspector=chart_inspector,
		data_analysis_assistant=analysis_assistant,
	)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert runtime.executed == []
	assert chart_inspector.calls == [
		{
			'runtime': runtime,
			'task': 'Find the independently verified result.',
			'page_url': 'https://example.com/result',
			'page_title': '',
			'cursor': None,
			'task_dir': tmp_path,
			'task_identity': _task_with_ground_truth().prompt_payload(),
		}
	]
	assert analysis_assistant.calls == [{'analysis_query': query, 'data_dir': data_dir}]
	assert outcome.usage == {'input_tokens': 11, 'output_tokens': 7}
	assert chart_output in outcome.steps[0]['outcome']
	assert analysis_output in outcome.steps[1]['outcome']
	model_text = _model_text(llm.calls)
	assert 'call_data_analysis_assistant' in model_text
	assert data_dir in model_text


@pytest.mark.asyncio
async def test_agent_rejects_analysis_directory_not_returned_by_ready_find(tmp_path: Path):
	data_dir = tmp_path / 'chart_data' / 'unregistered'
	data_dir.mkdir(parents=True)
	analysis_assistant = FakeDataAnalysisAssistant('{"status":"ok","answer":"must not run"}')
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(
					action='call_data_analysis_assistant',
					analysis_query='Analyze it',
					data_dir=str(data_dir.resolve()),
					thought='Try an unregistered directory.',
				)
			]
		),
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		max_steps=1,
		data_analysis_assistant=analysis_assistant,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MAX_STEPS'
	assert analysis_assistant.calls == []
	assert 'invalid_data_dir' in outcome.steps[0]['outcome']


@pytest.mark.asyncio
async def test_agent_rejects_ready_artifact_with_conflicting_year_filter(tmp_path: Path):
	data_dir = tmp_path / 'chart_data' / 'scan-2024'
	data_dir.mkdir(parents=True)
	chart_inspector = FakeChartNetworkInspector(
		json.dumps(
			{
				'action': 'find_chart_data_requests',
				'status': 'ready',
				'data_dir': str(data_dir.resolve()),
				'manifest_sha256': 'b' * 64,
				'active_filters': {'Year': '2024'},
			}
		)
	)
	analysis_assistant = FakeDataAnalysisAssistant('{"status":"ok","answer":"must not run"}')
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(action='find_chart_data_requests', thought='Save the chart.'),
				AgentDecision(
					action='call_data_analysis_assistant',
					analysis_query='Analyze the requested 2023 values.',
					data_dir=str(data_dir.resolve()),
					thought='Attempt stale analysis.',
				),
			]
		),
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		max_steps=2,
		chart_network_inspector=chart_inspector,
		data_analysis_assistant=analysis_assistant,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MAX_STEPS'
	assert analysis_assistant.calls == []
	assert 'conflicts with requested year' in outcome.steps[1]['outcome']


def test_chart_actions_share_deadline_and_cursor_keeps_a_small_fallback_budget(tmp_path: Path):
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM([]),
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		task_deadline_monotonic=time.monotonic() + 100,
	)

	assert agent._chart_action_budget('find_chart_data_requests') == 0
	assert 4.5 <= agent._chart_action_budget('find_chart_data_requests', cursor=True) <= 5
	assert 69 <= agent._chart_action_budget('call_data_analysis_assistant') <= 70


@pytest.mark.asyncio
async def test_saved_raw_cursor_page_does_not_count_as_a_new_failed_scan(tmp_path: Path):
	chart_inspector = FakeChartNetworkInspector(
		'{"action":"find_chart_data_requests","status":"saved_raw_only","page_index":0,"next_cursor":null}'
	)
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(action='find_chart_data_requests', cursor='scan:0', thought='Read one saved packet page.'),
				AgentDecision(
					action='finish',
					answer='The browser-grounded fallback succeeded.',
					evidence=['The saved packet page was read without rescanning.'],
					success=True,
				),
			]
		),
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		chart_network_inspector=chart_inspector,
		max_consecutive_action_errors=1,
	)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert chart_inspector.calls[0]['cursor'] == 'scan:0'
