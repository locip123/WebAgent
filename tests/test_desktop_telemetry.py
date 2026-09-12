import asyncio

from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation
from browser_use.webretriever.desktop.telemetry import RunTelemetry
from browser_use.webretriever.exploration_paths import PathJsonAction, PathJsonAddOperation
from browser_use.webretriever.models import AgentDecision, CompetitionTask
from browser_use.webretriever.run_control import RunnerEvent


def test_public_step_event_keeps_bounded_thought_without_private_payload_fields() -> None:
	emitted = []

	async def capture(event) -> None:
		emitted.append(event)

	thought = "t" * 4_001
	event = RunnerEvent(
		type="task.step.completed",
		task_id="task-4",
		task_idx=4,
		payload={
			"step": 2,
			"max_steps": 5,
			"action": "find_text",
			"outcome": "ok",
			"thought": thought,
			"prompt": "private prompt",
			"completion": "private completion",
			"screenshot": "private screenshot",
		},
	)

	asyncio.run(RunTelemetry(capture).on_event(event))

	assert len(emitted) == 1
	assert emitted[0].payload == {
		"step": 2,
		"max_steps": 5,
		"action": "find_text",
		"outcome": "ok",
		"thought": thought[:4_000],
	}


def test_public_decided_step_event_keeps_the_thought() -> None:
	emitted = []

	async def capture(event) -> None:
		emitted.append(event)

	asyncio.run(
		RunTelemetry(capture).on_event(
			RunnerEvent(
				type="task.step.decided",
				task_id="task-4",
				task_idx=4,
				payload={"step": 2, "max_steps": 5, "action": "find_text", "thought": "Inspect the page."},
			)
		)
	)

	assert len(emitted) == 1
	assert emitted[0].payload["thought"] == "Inspect the page."


def test_agent_step_event_contains_the_decision_thought(tmp_path) -> None:
	class RecordingObserver:
		def __init__(self) -> None:
			self.events = []

		async def on_event(self, event) -> None:
			self.events.append(event)

	class FakeRuntime:
		async def observe(self, step: int) -> BrowserObservation:
			return BrowserObservation(
				screenshot=b"",
				url="https://example.com",
				title="Example",
				tabs=[],
				viewport_width=1280,
				viewport_height=720,
				elements=[],
				page_text="Example page",
				recent_network=[],
				downloads=[],
			)

		async def execute(self, payload):
			return "ok"

	class FakeLlm:
		model = "test-model"

	thought = "Inspect the page before continuing."
	decision = AgentDecision(
		action="wait",
		thought=thought,
		seconds=1,
		current_path_id="1",
		decision_summary="Inspect the starting page before continuing.",
		path_json_action=PathJsonAction(
			operations=[
				PathJsonAddOperation(
					op="add",
					parent_path_id="1",
					location="Example page",
					strategy_description="Inspect the page for the requested information.",
				)
			]
		),
	)
	observer = RecordingObserver()
	agent = ProtocolIIIAgent(
		task=CompetitionTask(
			task_idx=0,
			task_id="task-0",
			website="https://example.com",
			task="inspect the page",
		),
		llm=FakeLlm(),
		runtime=FakeRuntime(),
		task_dir=tmp_path,
		max_steps=1,
		observer=observer,
	)

	async def request_decision(**_kwargs):
		return decision, {}, 0, 0.0

	agent._request_model_decision = request_decision
	asyncio.run(agent.run())

	step_events = [event for event in observer.events if event.type.startswith("task.step.")]
	assert [event.type for event in step_events] == ["task.step.decided", "task.step.completed"]
	assert step_events[0].payload["thought"] == thought
	assert step_events[1].payload["thought"] == thought
