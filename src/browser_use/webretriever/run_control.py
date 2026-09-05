"""Framework-independent observation and cooperative cancellation seams."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RunnerEvent:
	"""A sanitized event emitted by the Runner without control-plane metadata."""

	type: str
	payload: Mapping[str, Any] = field(default_factory=dict)
	worker_id: int | None = None
	task_id: str | None = None
	task_idx: int | None = None


class RunObserver(Protocol):
	"""Receive optional Runner lifecycle and progress events."""

	async def on_event(self, event: RunnerEvent) -> None: ...


class NoOpRunObserver:
	"""Preserve the legacy Runner path when no control plane is attached."""

	async def on_event(self, event: RunnerEvent) -> None:
		return None


class CancellationToken:
	"""A cooperative, process-local cancellation request owned by the caller."""

	def __init__(self) -> None:
		self._cancelled = False
		self._reason: str | None = None

	@property
	def cancelled(self) -> bool:
		return self._cancelled

	@property
	def reason(self) -> str:
		return self._reason or "cancellation requested"

	def cancel(self, reason: str | None = None) -> bool:
		"""Request cancellation once and retain the first caller-supplied reason."""

		if self._cancelled:
			return False
		self._cancelled = True
		self._reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
		return True


async def emit_safely(observer: RunObserver, event: RunnerEvent, *, logger: logging.Logger) -> None:
	"""Deliver telemetry without allowing observer failure to change Runner work."""

	try:
		await observer.on_event(event)
	except Exception as exc:
		logger.warning("Run observer rejected %s telemetry: %s", event.type, type(exc).__name__)


__all__ = ["CancellationToken", "NoOpRunObserver", "RunObserver", "RunnerEvent", "emit_safely"]
