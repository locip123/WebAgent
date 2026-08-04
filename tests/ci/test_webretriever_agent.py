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
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE, build_step_prompt_trace, build_system_prompt
from browser_use.webretriever.strategy import STRATEGY_CHECKPOINT_INTERVAL, ExplorationCheckpointTracker, StrategyCheckpointError


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


class NavigatingRuntime(FakeRuntime):
	def __init__(self, start_url: str = 'https://example.com/start') -> None:
		super().__init__()
		self.current_url = start_url

	async def observe(self, step: int) -> FakeObservation:
		self.observed_steps.append(step)
		return FakeObservation(url=self.current_url)

	async def execute(self, decision: AgentDecision) -> str:
		self.executed.append(decision)
		if decision.action == 'navigate' and decision.url is not None:
			self.current_url = decision.url
		return 'Action completed.'


@dataclass(slots=True)
class ChangingObservation(FakeObservation):
	marker: str = 'first-state'

	def render_text(self) -> str:
		return f'Visible browser state: {self.marker}'


class ChangingObservationRuntime(FakeRuntime):
	async def observe(self, step: int) -> ChangingObservation:
		self.observed_steps.append(step)
		return ChangingObservation(marker='second-state' if step >= 2 else 'first-state')


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
	def __init__(self, decisions: list[AgentDecision], *, auto_complete_checkpoints: bool = True) -> None:
		self.decisions = iter(decisions)
		self.calls: list[tuple[list[Any], Any]] = []
		self.auto_complete_checkpoints = auto_complete_checkpoints

	def _next_completion(self, messages: list[Any]) -> AgentDecision:
		decision = next(self.decisions)
		prompt = messages[-1].text
		if self.auto_complete_checkpoints and '===== REQUIRED ' in prompt and ' STRATEGY REVIEW =====' in prompt:
			missing = {
				field: value
				for field, value in _checkpoint_fields('AUTO-CHECKPOINT').items()
				if getattr(decision, field) is None
			}
			if missing:
				return decision.model_copy(update=missing)
		return decision

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		return SimpleNamespace(completion=self._next_completion(messages), usage=None)


class PromptLogInspectingLLM(FakeLLM):
	def __init__(self, decisions: list[AgentDecision], prompt_log_path: Path) -> None:
		super().__init__(decisions)
		self.prompt_log_path = prompt_log_path
		self.persisted_before_requests: list[dict[str, Any]] = []

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		prompt_log = json.loads(self.prompt_log_path.read_text(encoding='utf-8'))
		self.persisted_before_requests.append(prompt_log['steps'][-1])
		self.calls.append((messages, output_format))
		return SimpleNamespace(
			completion=self._next_completion(messages),
			usage=SimpleNamespace(input_tokens=3, output_tokens=2),
		)


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
		return SimpleNamespace(completion=self._next_completion(messages), usage=None)


class InvalidOnceLLM(FakeLLM):
	def __init__(self, decisions: list[AgentDecision]) -> None:
		super().__init__(decisions)
		self.invalid_returned = False

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		if not self.invalid_returned:
			self.invalid_returned = True
			raise ModelProviderError('1 validation error for AgentDecision\naction\n  Field required')
		return SimpleNamespace(completion=self._next_completion(messages), usage=None)


class FirstActionThenHangingLLM:
	def __init__(self) -> None:
		self.calls = 0
		self.second_call_started = asyncio.Event()

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls += 1
		if self.calls == 1:
			return SimpleNamespace(
				completion=AgentDecision(
					action='wait',
					seconds=0.1,
					thought='Record this action first.',
					**_checkpoint_fields('FIRST-ACTION-CHECKPOINT'),
				),
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


def test_system_prompt_keeps_conditional_bls_recovery_out_of_the_stable_core() -> None:
	prompt = build_system_prompt()

	assert 'https://api.bls.gov/publicAPI/v2/timeseries/data/<SERIES_ID>' not in prompt
	assert 'CES5000000001' not in prompt
	assert '2895' not in prompt


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
async def test_agent_writes_line_oriented_model_prompts_by_default(tmp_path: Path):
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
	).run()

	assert outcome.status == 'SUCCESS'
	prompt_log = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	assert prompt_log['format'] == 'webretriever-model-prompts/v2-lines'
	assert prompt_log['system_prompt'] == llm.calls[0][0][0].text.split('\n')
	assert prompt_log['steps'][0]['prompt'] == llm.calls[0][0][1].text.split('\n')
	assert '\n'.join(prompt_log['steps'][0]['prompt']) == llm.calls[0][0][1].text
	assert set(prompt_log['steps'][0]) == {'step', 'prompt', 'image'}
	assert 'SECRET_GROUND_TRUTH_9f6a' not in json.dumps(prompt_log, ensure_ascii=False)
	strategy_review_log = json.loads((tmp_path / 'strategy_review_prompts.json').read_text(encoding='utf-8'))
	assert strategy_review_log['format'] == 'webretriever-strategy-review-prompts/v1-lines'
	assert strategy_review_log['system_prompt'] == llm.calls[0][0][0].text.split('\n')
	assert len(strategy_review_log['reviews']) == 1
	assert strategy_review_log['reviews'][0] == {
		'step': 1,
		'trigger': 'initial_page',
		'completed_decisions': 0,
		'trajectory_decision_count': 0,
		'prompt': llm.calls[0][0][1].text.split('\n'),
		'image': {'media_type': 'image/png', 'detail': 'high', 'path': 'trajectory/0.png'},
	}
	assert 'SECRET_GROUND_TRUTH_9f6a' not in json.dumps(strategy_review_log, ensure_ascii=False)


@pytest.mark.asyncio
async def test_agent_can_write_structured_model_prompts_before_request(tmp_path: Path):
	llm = PromptLogInspectingLLM(
		[
			AgentDecision(action='wait', seconds=0.1, thought='Wait for the page to settle.'),
			AgentDecision(
				action='finish',
				answer='42',
				evidence=['The current page states 42.'],
				success=True,
			),
		],
		tmp_path / 'model_prompts.json',
	)

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		max_steps=2,
		structured_prompt_log=True,
	).run()

	assert outcome.status == 'SUCCESS'
	prompt_log = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	assert prompt_log['format'] == 'webretriever-model-prompts/v2-structured'
	assert prompt_log['metadata']['message_order'] == ['system_prompt', 'steps[].prompt', 'steps[].image']
	assert prompt_log['task'] == _task_with_ground_truth().prompt_payload()
	assert prompt_log['system_prompt']['rendered_text'] == llm.calls[0][0][0].text
	assert prompt_log['system_prompt']['metrics']['characters'] == len(llm.calls[0][0][0].text)
	assert [entry['step'] for entry in prompt_log['steps']] == [1, 2]
	assert [entry['prompt']['rendered_text'] for entry in prompt_log['steps']] == [call[0][1].text for call in llm.calls]
	assert [section['id'] for section in prompt_log['steps'][0]['prompt']['sections']] == [
		'authoritative_task',
		'execution_state',
		'browser_observation',
		'trusted_operational_guidance',
		'decision_instructions',
	]
	assert prompt_log['steps'][0]['prompt']['sections'][2]['fields']['rendered_text'] == (
		'Visible page text: independently verified result is 42.'
	)
	assert prompt_log['steps'][0]['prompt']['metrics']['estimated_tokens'] > 0
	assert prompt_log['steps'][0]['prompt']['metrics']['selected_playbooks'] == []
	assert prompt_log['steps'][0]['model_call']['duration_seconds'] >= 0
	assert prompt_log['steps'][0]['model_call']['usage'] == {'input_tokens': 3, 'output_tokens': 2}
	assert prompt_log['steps'][0]['model_call']['error'] is None
	assert all('model_call' not in entry for entry in llm.persisted_before_requests)
	assert [entry['image'] for entry in prompt_log['steps']] == [
		{'media_type': 'image/png', 'detail': 'high', 'path': 'trajectory/0.png'},
		{'media_type': 'image/png', 'detail': 'high', 'path': 'trajectory/1.png'},
	]
	assert 'SECRET_GROUND_TRUTH_9f6a' not in json.dumps(prompt_log, ensure_ascii=False)


def test_step_prompt_trace_structures_rendered_browser_observation() -> None:
	trace = build_step_prompt_trace(
		task='Read the visible result.',
		website='https://example.com/start',
		step=2,
		max_steps=100,
		observation=(
			'URL: https://example.com/result\n\nTitle: Result\n\nViewport: 1440x900\n\nTabs:\n'
			"  0: 'Result' https://example.com/result [active]\n\nInteractive elements:\n"
			'  [3] button name="Next"\n\nRecent XHR/Fetch:\n  GET https://example.com/api [200]\n\n'
			'Downloads:\n  (none)\n\nPage text:\nVerified result: 42'
		),
		history=[{'step': 1, 'thought': 'Opened the result.', 'outcome': 'Navigation completed.'}],
		memory='Verified: source is open.',
		last_outcome='Navigation completed.',
	)

	sections = {section['id']: section for section in trace.sections}
	assert trace.text.startswith('===== AUTHORITATIVE TASK =====')
	assert sections['authoritative_task']['fields']['starting_website'] == 'https://example.com/start'
	assert sections['execution_state']['fields']['step'] == 3
	assert sections['execution_state']['fields']['recent_trajectory'][0]['step'] == 1
	assert sections['browser_observation']['trust'] == 'untrusted_browser_content'
	assert sections['browser_observation']['fields']['viewport'] == {'text': '1440x900', 'width': 1440, 'height': 900}
	assert sections['browser_observation']['fields']['page_text'] == 'Verified result: 42'
	assert sections['decision_instructions']['text'].startswith('The attached image is the current Playwright screenshot')


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
		structured_prompt_log=True,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MODEL_TIMEOUT'
	assert outcome.error is not None and '0.01 seconds' in outcome.error
	assert runtime.executed == []
	assert (tmp_path / 'trajectory_visual' / '0.png').is_file()
	prompt_log = json.loads((tmp_path / 'model_prompts.json').read_text(encoding='utf-8'))
	assert 'exceeded 0.01 seconds' in prompt_log['steps'][0]['model_call']['error']


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
async def test_agent_blocks_third_identical_action_on_unchanged_observation(tmp_path: Path):
	decision = AgentDecision(action='wait', seconds=0.1, thought='Wait for the same page.')
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				decision,
				decision,
				decision,
				AgentDecision(
					action='finish',
					answer='42',
					evidence=['The current page states 42.'],
					success=True,
				),
			]
		),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=4,
	)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert len(runtime.executed) == 2
	blocked = json.loads(outcome.steps[2]['outcome'])
	assert blocked['status'] == 'repeated_unchanged_action'
	assert blocked['repeat_count'] == 3


@pytest.mark.asyncio
async def test_agent_resets_identical_action_count_when_action_changes(tmp_path: Path):
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(action='wait', seconds=0.1),
				AgentDecision(action='wait', seconds=0.1),
				AgentDecision(action='wait', seconds=0.2),
				AgentDecision(action='wait', seconds=0.1),
			],
		),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=4,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MAX_STEPS'
	assert len(runtime.executed) == 4
	assert all('repeated_unchanged_action' not in step['outcome'] for step in outcome.steps)


@pytest.mark.asyncio
async def test_agent_resets_identical_action_count_when_observation_changes(tmp_path: Path):
	runtime = ChangingObservationRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM([AgentDecision(action='wait', seconds=0.1) for _ in range(3)]),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=3,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_MAX_STEPS'
	assert len(runtime.executed) == 3
	assert all('repeated_unchanged_action' not in step['outcome'] for step in outcome.steps)


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
@pytest.mark.parametrize('status', ['no_match', 'saved_raw_only'])
async def test_unavailable_chart_scan_does_not_consume_action_error_budget(tmp_path: Path, status: str):
	chart_inspector = FakeChartNetworkInspector(
		json.dumps({'action': 'find_chart_data_requests', 'status': status, 'requests': [], 'datasets': []})
	)
	runtime = FakeRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(action='find_chart_data_requests', thought='Check for normalized chart data.'),
				AgentDecision(action='wait', seconds=0.1, thought='Read the visible chart instead.'),
				AgentDecision(
					action='finish',
					answer='The chart-visible fallback succeeded.',
					evidence=['The visible chart was read after no normalized chart data was available.'],
					success=True,
				),
			]
		),
		runtime=runtime,
		task_dir=tmp_path,
		chart_network_inspector=chart_inspector,
		max_consecutive_action_errors=1,
	)

	outcome = await agent.run()

	assert outcome.status == 'SUCCESS'
	assert chart_inspector.calls[0]['cursor'] is None
	assert [decision.action for decision in runtime.executed] == ['wait']


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


@dataclass(slots=True)
class ScrollObservation(FakeObservation):
	marker: str = 'top'

	def render_text(self) -> str:
		return f'Visible browser state: {self.marker}'


class OscillatingRuntime(FakeRuntime):
	"""Every scroll changes the observation, so exact-repeat guards never fire."""

	async def observe(self, step: int) -> ScrollObservation:
		self.observed_steps.append(step)
		return ScrollObservation(marker='bottom' if step % 2 else 'top')


@pytest.mark.asyncio
async def test_agent_interrupts_two_action_oscillation_cycle(tmp_path: Path):
	runtime = OscillatingRuntime()
	decisions = []
	for index in range(12):
		direction = 'up' if index % 2 else 'down'
		decisions.append(AgentDecision(action='scroll', direction=direction, pages=1, thought=f'Scroll {direction}.'))
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(decisions),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=13,
		max_consecutive_action_errors=99,
	)

	outcome = await agent.run()

	loop_steps = [step for step in outcome.steps if 'loop_detected' in str(step.get('outcome', ''))]
	assert loop_steps, 'an A<->B oscillation must be interrupted before the deadline'
	blocked = json.loads(loop_steps[0]['outcome'])
	assert blocked['status'] == 'loop_detected'
	assert blocked['pattern'] == 'oscillation'
	assert len(runtime.executed) < 12


@pytest.mark.asyncio
async def test_agent_interrupts_near_duplicate_probe_family(tmp_path: Path):
	runtime = OscillatingRuntime()
	decisions = [
		AgentDecision(action='inspect_network', text=f'query-{index}', thought='Search captured bodies.') for index in range(10)
	]
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(decisions),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=11,
		max_consecutive_action_errors=99,
	)

	outcome = await agent.run()

	loop_steps = [step for step in outcome.steps if 'loop_detected' in str(step.get('outcome', ''))]
	assert loop_steps, 'a same-action probe family with only parameter changes must be interrupted'
	blocked = json.loads(loop_steps[0]['outcome'])
	assert blocked['status'] == 'loop_detected'
	assert blocked['pattern'] == 'unproductive_probe_family'
	assert blocked['action'] == 'inspect_network'


@pytest.mark.asyncio
async def test_agent_interrupts_drag_retries_that_only_jitter_coordinates(tmp_path: Path):
	"""Case 95 dragged one date-picker column with endpoints varying a few pixels."""

	runtime = OscillatingRuntime()
	decisions = []
	for index in range(12):
		if index % 2:
			decisions.append(AgentDecision(action='click', element_id=index, thought='Open the day column.'))
		else:
			decisions.append(
				AgentDecision(
					action='drag',
					x=900,
					y=610,
					end_x=900 + index,
					end_y=500 + index,
					thought='Drag the day column upward.',
				)
			)
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(decisions),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=13,
		max_consecutive_action_errors=99,
	)

	outcome = await agent.run()

	loop_steps = [step for step in outcome.steps if 'loop_detected' in str(step.get('outcome', ''))]
	assert loop_steps, 'drag retries differing only by a few pixels must not read as fresh intents'
	assert len(runtime.executed) < 12


@pytest.mark.asyncio
async def test_agent_interrupts_non_alternating_intent_churn(tmp_path: Path):
	"""Case 62 rotated find_text/click/scroll over one form without ever advancing."""

	runtime = OscillatingRuntime()
	rotation = [
		AgentDecision(action='find_text', text='Submit', thought='Locate the control.'),
		AgentDecision(action='click', element_id=7, thought='Click the control.'),
		AgentDecision(action='scroll', direction='down', pages=1, thought='Look further down.'),
	]
	decisions = [rotation[index % 3].model_copy() for index in range(12)]
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(decisions),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=13,
		max_consecutive_action_errors=99,
	)

	outcome = await agent.run()

	loop_steps = [step for step in outcome.steps if 'loop_detected' in str(step.get('outcome', ''))]
	assert loop_steps, 'a three-intent rotation over one state must be interrupted'
	blocked = json.loads(loop_steps[0]['outcome'])
	assert blocked['pattern'] == 'intent_churn'
	assert blocked['distinct_intents'] <= 3


@pytest.mark.asyncio
async def test_detect_loop_stays_quiet_on_progressing_exploration(tmp_path: Path):
	"""Healthy exploration keeps changing state, so no pattern may fire."""

	runtime = FakeRuntime()
	decisions = [
		AgentDecision(action='navigate', url=f'https://example.test/page-{index}', thought='Open the next source.')
		for index in range(12)
	]
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(decisions),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=13,
		max_consecutive_action_errors=99,
	)

	outcome = await agent.run()

	assert not [step for step in outcome.steps if 'loop_detected' in str(step.get('outcome', ''))]


class SlowActionRuntime(FakeRuntime):
	"""Executes one action slowly enough to cross the task deadline."""

	async def execute(self, decision: AgentDecision) -> str:
		self.executed.append(decision)
		await asyncio.sleep(0.2)
		return 'Action completed.'


@pytest.mark.asyncio
async def test_agent_salvages_verified_memory_when_task_deadline_elapses(tmp_path: Path):
	runtime = SlowActionRuntime()
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=FakeLLM(
			[
				AgentDecision(
					action='wait',
					seconds=0.1,
					thought='Record the verified value.',
					memory='Verified: 2022/08 sugar price change is -13.7 percent (askci monthly article).',
				)
			]
		),
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=5,
		task_deadline_monotonic=time.monotonic() + 0.05,
	)

	outcome = await agent.run()

	assert outcome.status == 'FAIL_TASK_TIMEOUT'
	assert outcome.agent_answer, 'verified facts must be salvaged instead of returning an empty answer'
	assert '-13.7' in outcome.agent_answer
	assert outcome.evidence


@pytest.mark.asyncio
async def test_agent_reports_remaining_task_seconds_to_the_model(tmp_path: Path):
	llm = FakeLLM([AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True)])
	agent = ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		max_steps=3,
		task_deadline_monotonic=time.monotonic() + 240.0,
	)

	await agent.run()

	assert 'Remaining task time' in _model_text(llm.calls)


def _checkpoint_fields(marker: str) -> dict[str, str]:
	return {
		'checkpoint_strategy_catalog': f'- {marker}: tried navigation.\n- {marker}: untried official table and export routes.',
		'checkpoint_active_strategy': f'- {marker}: inspect the official table route.',
		'checkpoint_confirmed_infeasible': f'- {marker}: no strategy is confirmed infeasible.',
		'checkpoint_next_strategies': f'- {marker}: table first.\n- {marker}: export second.',
	}


@pytest.mark.asyncio
async def test_agent_refreshes_persistent_strategy_checkpoint_on_initial_page_and_after_twenty_decisions(tmp_path: Path):
	decisions = [
		AgentDecision(
			action='navigate',
			url='https://example.com/route-1',
			thought='Create the initial strategy checkpoint and begin.',
			**_checkpoint_fields('INITIAL-CHECKPOINT'),
		)
	]
	decisions.extend(
		AgentDecision(action='navigate', url=f'https://example.com/route-{index}', thought=f'Open route {index}.')
		for index in range(2, 21)
	)
	decisions.append(
		AgentDecision(
			action='navigate',
			url='https://example.com/route-21',
			thought='Refresh the periodic strategy checkpoint and continue.',
			**_checkpoint_fields('PERIODIC-CHECKPOINT'),
		)
	)
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	llm = FakeLLM(decisions)
	runtime = FakeRuntime()

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=22,
	).run()

	assert outcome.status == 'SUCCESS'
	assert len(llm.calls) == 22, 'the review must share the normal model call rather than add one'
	model_texts = [call[0][1].text for call in llm.calls]
	assert 'REQUIRED INITIAL-PAGE STRATEGY REVIEW' in model_texts[0]
	assert 'REQUIRED 20-DECISION STRATEGY REVIEW' not in model_texts[19]
	assert 'REQUIRED 20-DECISION STRATEGY REVIEW' in model_texts[20]
	assert '"decision":1' in model_texts[20]
	assert '"decision":20' in model_texts[20]
	assert 'PERIODIC-CHECKPOINT' in model_texts[21]
	assert all('checkpoint_' not in action for action in outcome.actions)
	assert len(runtime.executed) == 21


@pytest.mark.asyncio
async def test_agent_refreshes_strategy_checkpoint_immediately_after_entering_a_new_page(tmp_path: Path):
	llm = FakeLLM(
		[
			AgentDecision(
				action='navigate',
				url='https://example.com/results#top',
				thought='Plan from the starting page and open the result page.',
				**_checkpoint_fields('INITIAL-CHECKPOINT'),
			),
			AgentDecision(
				action='finish',
				answer='42',
				evidence=['The result page states 42.'],
				success=True,
				thought='Replan on the entered page before returning the grounded answer.',
				**_checkpoint_fields('PAGE-ENTRY-CHECKPOINT'),
			),
		]
	)
	runtime = NavigatingRuntime()

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=2,
	).run()

	assert outcome.status == 'SUCCESS'
	assert len(llm.calls) == 2
	assert 'REQUIRED INITIAL-PAGE STRATEGY REVIEW' in llm.calls[0][0][1].text
	page_prompt = llm.calls[1][0][1].text
	assert 'REQUIRED PAGE-ENTRY STRATEGY REVIEW' in page_prompt
	assert '"decision":1' in page_prompt
	assert len(runtime.executed) == 1


@pytest.mark.asyncio
async def test_agent_counts_locally_blocked_repeated_decisions_in_the_strategy_review(tmp_path: Path):
	decisions = [
		AgentDecision(
			action='navigate',
			url='https://example.com/initial-route',
			thought='Create the initial strategy checkpoint.',
			**_checkpoint_fields('INITIAL-CHECKPOINT'),
		)
	]
	decisions.extend(AgentDecision(action='wait', seconds=0.1, thought='Wait for the unchanged page.') for _ in range(19))
	decisions.append(
		AgentDecision(
			action='navigate',
			url='https://example.com/recovery',
			thought='Switch strategy after the repeated probes.',
			**_checkpoint_fields('PERIODIC-CHECKPOINT'),
		)
	)
	decisions.append(AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True))
	llm = FakeLLM(decisions)
	runtime = FakeRuntime()

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=22,
		max_consecutive_action_errors=99,
	).run()

	assert outcome.status == 'SUCCESS'
	checkpoint_prompt = llm.calls[20][0][1].text
	assert 'REQUIRED 20-DECISION STRATEGY REVIEW' in checkpoint_prompt
	assert 'repeated_unchanged_action' in checkpoint_prompt
	assert len(runtime.executed) == 4, 'locally blocked decisions count but do not execute another browser action'


@pytest.mark.asyncio
async def test_agent_retries_the_same_checkpoint_when_the_four_fields_are_missing(tmp_path: Path):
	decisions = [
		AgentDecision(
			action='navigate',
			url='https://example.com/route-1',
			thought='Create the initial strategy checkpoint.',
			**_checkpoint_fields('INITIAL-CHECKPOINT'),
		)
	]
	decisions.extend(
		AgentDecision(action='navigate', url=f'https://example.com/route-{index}', thought=f'Open route {index}.')
		for index in range(2, 21)
	)
	decisions.extend(
		[
			AgentDecision(
				action='navigate',
				url='https://example.com/missing-checkpoint',
				thought='This response deliberately omits the required checkpoint fields.',
			),
			AgentDecision(
				action='navigate',
				url='https://example.com/accepted-checkpoint',
				thought='Provide the required checkpoint fields and continue.',
				**_checkpoint_fields('RECOVERED-CHECKPOINT'),
			),
			AgentDecision(action='finish', answer='42', evidence=['The page states 42.'], success=True),
		]
	)
	llm = FakeLLM(decisions, auto_complete_checkpoints=False)
	runtime = FakeRuntime()

	outcome = await ProtocolIIIAgent(
		task=_task_with_ground_truth(),
		llm=llm,
		runtime=runtime,
		task_dir=tmp_path,
		max_steps=23,
	).run()

	assert outcome.status == 'SUCCESS'
	assert 'REQUIRED 20-DECISION STRATEGY REVIEW' in llm.calls[20][0][1].text
	assert 'REQUIRED 20-DECISION STRATEGY REVIEW' in llm.calls[21][0][1].text
	assert all(decision.url != 'https://example.com/missing-checkpoint' for decision in runtime.executed)
	assert 'RECOVERED-CHECKPOINT' in llm.calls[22][0][1].text
	strategy_review_log = json.loads((tmp_path / 'strategy_review_prompts.json').read_text(encoding='utf-8'))
	assert [(entry['step'], entry['trigger']) for entry in strategy_review_log['reviews']] == [
		(1, 'initial_page'),
		(21, 'periodic'),
		(22, 'periodic'),
	]
	assert [
		'\n'.join(entry['prompt']) for entry in strategy_review_log['reviews']
	] == [llm.calls[index][0][1].text for index in (0, 20, 21)]


def _strategy_record(url: str) -> dict[str, object]:
	return {
		'url': url,
		'title': 'Observed page',
		'page_observation': 'Browser-derived evidence.',
		'action': {'action': 'wait'},
		'outcome': 'Completed.',
	}


def _accept_strategy_review(tracker: ExplorationCheckpointTracker) -> None:
	tracker.accept_review(
		strategy_catalog='- Tried visible evidence.\n- An official table remains available.',
		active_strategy='- Inspect the current official page.',
		confirmed_infeasible='- No strategy is confirmed infeasible.',
		next_strategies='- Read the table.\n- Inspect a first-party export.',
	)


def test_strategy_tracker_rejects_non_list_checkpoint_fields() -> None:
	tracker = ExplorationCheckpointTracker()
	assert tracker.review_request(current_page_url='https://example.com/start') is not None

	with pytest.raises(StrategyCheckpointError, match='checkpoint_next_strategies must be a Markdown list'):
		tracker.accept_review(
			strategy_catalog='- [untried] Inspect the official table.',
			active_strategy='- Inspect the official table.',
			confirmed_infeasible='- None confirmed.',
			next_strategies='Inspect the first-party export next.',
		)

	assert tracker.checkpoint is None


def test_strategy_tracker_distinguishes_initial_page_entry_fragments_and_download_placeholders() -> None:
	tracker = ExplorationCheckpointTracker()

	initial = tracker.review_request(current_page_url='https://example.com/start#overview')
	assert initial is not None
	assert initial.trigger == 'initial_page'
	assert initial.completed_decisions == 0
	assert initial.trajectory == ()
	_accept_strategy_review(tracker)

	tracker.record_decision(_strategy_record('https://example.com/start#table'))
	assert tracker.review_request(current_page_url='https://example.com/start#footer') is None

	page_entry = tracker.review_request(current_page_url='https://example.com/results#top')
	assert page_entry is not None
	assert page_entry.trigger == 'page_entry'
	assert page_entry.completed_decisions == 1
	assert [item['decision'] for item in page_entry.trajectory] == [1]
	_accept_strategy_review(tracker)

	tracker.record_decision(_strategy_record('https://example.com/results'))
	assert tracker.review_request(current_page_url=':') is None
	tracker.record_decision(_strategy_record(':'))
	assert tracker.review_request(current_page_url='https://example.com/results') is None


def test_strategy_tracker_uses_twenty_decisions_and_resets_the_since_review_trajectory() -> None:
	tracker = ExplorationCheckpointTracker()
	assert tracker.review_request(current_page_url='https://example.com/start') is not None
	_accept_strategy_review(tracker)

	for _ in range(STRATEGY_CHECKPOINT_INTERVAL):
		tracker.record_decision(_strategy_record('https://example.com/start'))

	periodic = tracker.review_request(current_page_url='https://example.com/start')
	assert periodic is not None
	assert periodic.trigger == 'periodic'
	assert periodic.completed_decisions == STRATEGY_CHECKPOINT_INTERVAL
	assert len(periodic.trajectory) == STRATEGY_CHECKPOINT_INTERVAL
	assert periodic.trajectory[0]['decision'] == 1
	assert periodic.trajectory[-1]['decision'] == STRATEGY_CHECKPOINT_INTERVAL
	_accept_strategy_review(tracker)

	tracker.record_decision(_strategy_record('https://example.com/start'))
	page_entry = tracker.review_request(current_page_url='https://example.com/next')
	assert page_entry is not None
	assert page_entry.trigger == 'page_entry'
	assert page_entry.completed_decisions == STRATEGY_CHECKPOINT_INTERVAL + 1
	assert [item['decision'] for item in page_entry.trajectory] == [STRATEGY_CHECKPOINT_INTERVAL + 1]
