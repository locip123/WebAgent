"""Runner-observer adapter for durable, sanitized desktop events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from browser_use.webretriever.desktop.contracts import EventTaskReference, EventType, RunEventDraft
from browser_use.webretriever.run_control import RunObserver, RunnerEvent


_PUBLIC_RUNNER_EVENTS = frozenset(
	{
		"run.started",
		"worker.state_changed",
		"task.started",
		"task.phase_changed",
		"task.step.decided",
		"task.step.completed",
		"task.recovery",
		"artifact.available",
		"task.finished",
		"run.cancelled",
		"run.completed",
	}
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset({"api_key", "authorization", "token", "prompt", "completion", "screenshot"})
_MAX_PUBLIC_THOUGHT_CHARS = 4_000


class RunTelemetry(RunObserver):
	"""Translate framework-free RunnerEvent values into public event drafts."""

	def __init__(self, emit: Callable[[RunEventDraft], Awaitable[None]]) -> None:
		self._emit = emit

	async def on_event(self, event: RunnerEvent) -> None:
		if event.type not in _PUBLIC_RUNNER_EVENTS:
			return
		task = None
		if event.task_id is not None and event.task_idx is not None:
			task = EventTaskReference(task_id=event.task_id, task_idx=event.task_idx)
		await self._emit(
			RunEventDraft(
				type=cast(EventType, event.type),
				level="info",
				task=task,
				payload=sanitize_payload(event.payload),
			)
		)


def sanitize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
	"""Drop values whose names could expose credentials or model-private data."""

	sanitized: dict[str, Any] = {}
	for key, value in payload.items():
		name = str(key)
		normalized_name = name.casefold()
		if normalized_name in _FORBIDDEN_PAYLOAD_KEYS or normalized_name.endswith("_secret"):
			continue
		if normalized_name == "thought":
			if isinstance(value, str):
				sanitized[name] = value[:_MAX_PUBLIC_THOUGHT_CHARS]
			continue
		sanitized[name] = value
	return sanitized


__all__ = ["RunTelemetry", "sanitize_payload"]
