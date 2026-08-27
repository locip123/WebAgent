"""Run-scoped, load-aware routing for WebRetriever model services."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.webretriever.model_retry import (
	await_with_hard_timeout,
	invoke_with_reconnect_retries,
	is_retryable_model_error,
)

T = TypeVar('T', bound=BaseModel)

MODEL_SERVICE_COOLDOWN_BASE_SECONDS = 2.0
MODEL_SERVICE_COOLDOWN_MAX_SECONDS = 60.0
MODEL_SERVICE_AFFINITY_MAX_LOAD_DELTA = 1


@dataclass(frozen=True, slots=True)
class ModelServiceConfig:
	"""The endpoint-specific part of one shared-model service configuration."""

	name: str
	api_base: str
	api_key: str


@dataclass(slots=True)
class _ModelServiceState:
	config: ModelServiceConfig
	client: BaseChatModel
	failure_streak: int = 0
	active_request_count: int = 0
	cooldown_until_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class _ModelServiceLease:
	service: _ModelServiceState
	selection_reason: str
	preferred_service: str | None


class ModelServicesExhausted(ModelProviderError):
	"""Compatibility error for callers with no configured model services."""

	def __init__(self, attempts: Sequence[dict[str, Any]]) -> None:
		self.attempts = [dict(attempt) for attempt in attempts]
		details = '; '.join(
			f"{attempt.get('service', 'unknown')}: {attempt.get('error_type', 'error')}"
			for attempt in self.attempts
		)
		message = 'No model services are available'
		if details:
			message = f'{message}: {details}'
		super().__init__(message=message, status_code=503, model=None)


class ModelServicesTimedOut(TimeoutError):
	"""A model-call deadline elapsed before any service produced a response."""

	def __init__(self, attempts: Sequence[dict[str, Any]]) -> None:
		self.attempts = [dict(attempt) for attempt in attempts]
		details = '; '.join(
			f"{attempt.get('service', 'unknown')}: {attempt.get('error_type', 'timeout')}"
			for attempt in self.attempts
		)
		message = 'Model service recovery reached its deadline'
		if details:
			message = f'{message}: {details}'
		super().__init__(message)


class ModelServiceRouter:
	"""Select the least-loaded eligible service for every model invocation.

	One router is shared by every worker in a CLI run. A lease increments a
	service's in-flight count under a lock before the request starts, so concurrent
	workers cannot all choose the first configuration. A task keeps a soft
	preference for its first service to preserve prompt-cache locality, but yields
	when that service is cooling or two requests busier than the least-loaded one.
	Provider failures release the lease and place only that service into exponential
	cooldown; callers keep trying other or recovered services until their enclosing
	deadline cancels them.
	"""

	_verified_api_keys = True

	def __init__(
		self,
		*,
		model: str,
		services: Sequence[ModelServiceConfig],
		clients: Sequence[BaseChatModel],
		model_timeout_seconds: float,
	) -> None:
		if not services:
			raise ValueError('at least one model service is required')
		if len(services) != len(clients):
			raise ValueError('model service and client counts must match')
		if model_timeout_seconds <= 0:
			raise ValueError('model_timeout_seconds must be greater than 0')
		self.model = model
		self.model_timeout_seconds = model_timeout_seconds
		self._states = [_ModelServiceState(config, client) for config, client in zip(services, clients, strict=True)]
		self._events: list[dict[str, Any]] = []
		self._call_index = 0
		self._tie_breaker = 0
		self._task_affinities: dict[str, _ModelServiceState] = {}
		self._state_lock = asyncio.Lock()

	@property
	def provider(self) -> str:
		return 'model-service-router'

	@property
	def name(self) -> str:
		return self.model

	@property
	def model_name(self) -> str:
		return self.model

	@property
	def service_names(self) -> tuple[str, ...]:
		return tuple(state.config.name for state in self._states)

	@property
	def base_url(self) -> Any:
		"""Expose the first client's endpoint for legacy inspection code."""

		return getattr(self._states[0].client, 'base_url', None)

	@property
	def use_responses_api(self) -> bool:
		return bool(getattr(self._states[0].client, 'use_responses_api', False))

	@property
	def stream_responses_api(self) -> bool:
		return bool(getattr(self._states[0].client, 'stream_responses_api', False))

	@property
	def event_count(self) -> int:
		return len(self._events)

	def service_state_payload(self) -> list[dict[str, Any]]:
		now = time.monotonic()
		return [
			{
				'name': state.config.name,
				'failure_streak': state.failure_streak,
				'active_request_count': state.active_request_count,
				'cooldown_remaining_seconds': round(max(0.0, state.cooldown_until_monotonic - now), 3),
			}
			for state in self._states
		]

	def service_events_since(self, index: int) -> list[dict[str, Any]]:
		return [dict(event) for event in self._events[max(0, index) :]]

	async def clear_task_affinity(self, task_id: str) -> None:
		"""Forget a finished task's cache preference."""

		async with self._state_lock:
			self._task_affinities.pop(task_id, None)

	def _record_event(
		self,
		*,
		call_index: int,
		service: _ModelServiceState,
		status: str,
		duration_seconds: float,
		error: Exception | None,
		selection_reason: str,
		preferred_service: str | None,
		affinity_migrated: bool = False,
	) -> None:
		event: dict[str, Any] = {
			'call_index': call_index,
			'service': service.config.name,
			'status': status,
			'duration_seconds': round(max(0.0, duration_seconds), 3),
			'failure_streak': service.failure_streak,
			'active_request_count': service.active_request_count,
			'cooldown_remaining_seconds': round(max(0.0, service.cooldown_until_monotonic - time.monotonic()), 3),
			'selection_reason': selection_reason,
			'preferred_service': preferred_service,
			'affinity_migrated': affinity_migrated,
		}
		if error is not None:
			event['error_type'] = type(error).__name__
			event['error'] = str(error)[:1_000]
		self._events.append(event)

	async def _next_call_index(self) -> int:
		async with self._state_lock:
			self._call_index += 1
			return self._call_index

	async def _acquire_least_loaded_service(
		self,
		*,
		remaining_seconds: Callable[[], float],
		attempts: Sequence[dict[str, Any]],
		affinity_key: str | None,
	) -> _ModelServiceLease:
		"""Lease an eligible service, waiting for the earliest cooldown if needed."""

		while True:
			remaining = remaining_seconds()
			if remaining <= 0:
				raise ModelServicesTimedOut(attempts)
			now = time.monotonic()
			async with self._state_lock:
				eligible = [state for state in self._states if state.cooldown_until_monotonic <= now]
				if eligible:
					least_active = min(state.active_request_count for state in eligible)
					preferred = self._task_affinities.get(affinity_key) if affinity_key is not None else None
					preferred_is_eligible = any(state is preferred for state in eligible)
					if (
						preferred_is_eligible
						and preferred is not None
						and preferred.active_request_count <= least_active + MODEL_SERVICE_AFFINITY_MAX_LOAD_DELTA
					):
						service = preferred
						selection_reason = 'task_affinity'
					else:
						candidates = [state for state in eligible if state.active_request_count == least_active]
						service = candidates[self._tie_breaker % len(candidates)]
						self._tie_breaker += 1
						if affinity_key is None:
							selection_reason = 'least_loaded'
						elif preferred is None:
							selection_reason = 'initial_least_loaded'
						elif preferred_is_eligible:
							selection_reason = 'load_shed'
						else:
							selection_reason = 'preferred_cooling'
					if affinity_key is not None and preferred is None:
						self._task_affinities[affinity_key] = service
					service.active_request_count += 1
					return _ModelServiceLease(
						service=service,
						selection_reason=selection_reason,
						preferred_service=preferred.config.name if preferred is not None else None,
					)
				wait_seconds = min(state.cooldown_until_monotonic for state in self._states) - now
			await asyncio.sleep(min(max(0.0, wait_seconds), remaining))

	async def _release_success(self, service: _ModelServiceState, *, affinity_key: str | None) -> bool:
		"""Release a successful lease and migrate only from a cooled preference."""

		async with self._state_lock:
			service.active_request_count = max(0, service.active_request_count - 1)
			service.failure_streak = 0
			service.cooldown_until_monotonic = 0.0
			if affinity_key is None:
				return False
			preferred = self._task_affinities.get(affinity_key)
			if preferred is None:
				self._task_affinities[affinity_key] = service
				return False
			if preferred is not service and preferred.cooldown_until_monotonic > time.monotonic():
				self._task_affinities[affinity_key] = service
				return True
			return False

	async def _release_failure(self, service: _ModelServiceState, *, recoverable: bool) -> None:
		async with self._state_lock:
			service.active_request_count = max(0, service.active_request_count - 1)
			if not recoverable:
				return
			service.failure_streak += 1
			cooldown = min(
				MODEL_SERVICE_COOLDOWN_BASE_SECONDS * (2 ** (service.failure_streak - 1)),
				MODEL_SERVICE_COOLDOWN_MAX_SECONDS,
			)
			service.cooldown_until_monotonic = time.monotonic() + cooldown

	async def _release_cancelled(self, service: _ModelServiceState) -> None:
		async with self._state_lock:
			service.active_request_count = max(0, service.active_request_count - 1)

	async def ainvoke(
		self,
		messages: list[BaseMessage],
		output_format: type[T] | None = None,
		**kwargs: Any,
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		"""Invoke using a single-call deadline when called through the base API."""

		return await invoke_with_service_failover(
			self,
			lambda client: client.ainvoke(messages, output_format=output_format, **kwargs),
			timeout_seconds=self.model_timeout_seconds,
		)


def _remaining_timeout(timeout_seconds: float | Callable[[], float]) -> Callable[[], float]:
	"""Turn a static logical-call timeout into a decreasing deadline callback."""

	if callable(timeout_seconds):
		return timeout_seconds
	deadline = time.monotonic() + timeout_seconds
	return lambda: deadline - time.monotonic()


async def invoke_with_service_failover(
	router: ModelServiceRouter,
	invoke: Callable[[BaseChatModel], Awaitable[T]],
	*,
	timeout_seconds: float | Callable[[], float],
	on_attempt_started: Callable[[int, str], None] | None = None,
	on_attempt_finished: Callable[[int, str, str, float, Exception | None], None] | None = None,
	affinity_key: str | None = None,
) -> T:
	"""Recover model-service failures until the supplied deadline is exhausted."""

	remaining_seconds = _remaining_timeout(timeout_seconds)
	attempts: list[dict[str, Any]] = []
	call_index = await router._next_call_index()
	attempt_number = 0

	while True:
		lease = await router._acquire_least_loaded_service(
			remaining_seconds=remaining_seconds,
			attempts=attempts,
			affinity_key=affinity_key,
		)
		service = lease.service
		per_attempt_timeout = remaining_seconds()
		if per_attempt_timeout <= 0:
			await router._release_cancelled(service)
			raise ModelServicesTimedOut(attempts)
		attempt_number += 1
		if on_attempt_started is not None:
			on_attempt_started(attempt_number, service.config.name)
		started_at = time.monotonic()
		try:
			result = await await_with_hard_timeout(invoke(service.client), per_attempt_timeout)
		except asyncio.CancelledError:
			duration = time.monotonic() - started_at
			await router._release_cancelled(service)
			router._record_event(
				call_index=call_index,
				service=service,
				status='cancelled',
				duration_seconds=duration,
				error=None,
				selection_reason=lease.selection_reason,
				preferred_service=lease.preferred_service,
			)
			if on_attempt_finished is not None:
				on_attempt_finished(attempt_number, service.config.name, 'cancelled', duration, None)
			raise
		except Exception as exc:
			duration = time.monotonic() - started_at
			status = 'timed_out' if isinstance(exc, TimeoutError) else 'failed'
			recoverable = is_retryable_model_error(exc)
			await router._release_failure(service, recoverable=recoverable)
			router._record_event(
				call_index=call_index,
				service=service,
				status=status,
				duration_seconds=duration,
				error=exc,
				selection_reason=lease.selection_reason,
				preferred_service=lease.preferred_service,
			)
			if on_attempt_finished is not None:
				on_attempt_finished(attempt_number, service.config.name, status, duration, exc)
			if not recoverable:
				raise
			attempts.append(
				{
					'service': service.config.name,
					'status': status,
					'error_type': type(exc).__name__,
					'error': str(exc)[:1_000],
					'failure_streak': service.failure_streak,
					'cooldown_remaining_seconds': round(
						max(0.0, service.cooldown_until_monotonic - time.monotonic()), 3
					),
				}
			)
			continue
		else:
			duration = time.monotonic() - started_at
			affinity_migrated = await router._release_success(service, affinity_key=affinity_key)
			router._record_event(
				call_index=call_index,
				service=service,
				status='successful',
				duration_seconds=duration,
				error=None,
				selection_reason=lease.selection_reason,
				preferred_service=lease.preferred_service,
				affinity_migrated=affinity_migrated,
			)
			if on_attempt_finished is not None:
				on_attempt_finished(attempt_number, service.config.name, 'successful', duration, None)
			return result


async def invoke_model_call(
	llm: BaseChatModel,
	invoke: Callable[[BaseChatModel], Awaitable[T]],
	*,
	timeout_seconds: float | Callable[[], float],
	affinity_key: str | None = None,
) -> T:
	"""Use service routing when available and retain legacy retry behavior otherwise."""

	if isinstance(llm, ModelServiceRouter):
		return await invoke_with_service_failover(
			llm,
			invoke,
			timeout_seconds=timeout_seconds,
			affinity_key=affinity_key,
		)
	return await invoke_with_reconnect_retries(
		lambda: invoke(llm),
		timeout_seconds=timeout_seconds,
	)


__all__ = [
	'MODEL_SERVICE_COOLDOWN_BASE_SECONDS',
	'MODEL_SERVICE_COOLDOWN_MAX_SECONDS',
	'MODEL_SERVICE_AFFINITY_MAX_LOAD_DELTA',
	'ModelServiceConfig',
	'ModelServiceRouter',
	'ModelServicesExhausted',
	'ModelServicesTimedOut',
	'invoke_model_call',
	'invoke_with_service_failover',
]
