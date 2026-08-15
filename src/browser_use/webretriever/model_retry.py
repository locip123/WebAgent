"""Bounded fresh-connection retries for WebRetriever model invocations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from browser_use.llm.exceptions import ModelProviderError

T = TypeVar('T')

MODEL_RETRY_MAX_ATTEMPTS = 5
MODEL_RETRY_DELAY_SECONDS = 8.0
_RETRYABLE_MODEL_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _consume_detached_task_result(task: asyncio.Future[object]) -> None:
	"""Retrieve a late result so cancellation-resistant calls do not warn."""

	if task.cancelled():
		return
	try:
		task.exception()
	except BaseException:
		pass


async def await_with_hard_timeout(awaitable: Awaitable[T], timeout_seconds: float) -> T:
	"""Return at the deadline without waiting for a cancelled call to unwind."""

	if timeout_seconds <= 0:
		raise TimeoutError
	task = asyncio.ensure_future(awaitable)
	try:
		done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
	except BaseException:
		if not task.done():
			task.add_done_callback(_consume_detached_task_result)
			task.cancel()
		raise
	if task in done or task.done():
		return task.result()
	task.add_done_callback(_consume_detached_task_result)
	task.cancel()
	raise TimeoutError


def is_retryable_model_error(exc: Exception) -> bool:
	"""Identify transient connection, service, and timeout failures only."""

	if isinstance(exc, TimeoutError):
		return True
	if isinstance(exc, ConnectionError):
		return True
	if not isinstance(exc, ModelProviderError):
		return False
	message = str(exc)
	if 'validation error for ' in message or 'Failed to parse structured output' in message:
		return False
	return exc.status_code in _RETRYABLE_MODEL_STATUS_CODES


async def invoke_with_reconnect_retries(
	invoke: Callable[[], Awaitable[T]],
	*,
	timeout_seconds: float | Callable[[], float],
	on_attempt_finished: Callable[[int, str, float, Exception | None], None] | None = None,
	on_retry_wait_finished: Callable[[int, float], None] | None = None,
) -> T:
	"""Invoke through at most five fresh clients, waiting eight seconds to retry.

	The callable must create one new request per invocation.  ``ChatOpenAI``
	does so by constructing a fresh ``AsyncOpenAI`` client in ``ainvoke``.
	"""

	for attempt in range(1, MODEL_RETRY_MAX_ATTEMPTS + 1):
		per_attempt_timeout = timeout_seconds() if callable(timeout_seconds) else timeout_seconds
		if per_attempt_timeout <= 0:
			raise TimeoutError
		started_at = time.monotonic()
		try:
			result = await await_with_hard_timeout(invoke(), per_attempt_timeout)
		except asyncio.CancelledError:
			if on_attempt_finished is not None:
				on_attempt_finished(attempt, 'cancelled', time.monotonic() - started_at, None)
			raise
		except Exception as exc:
			status = 'timed_out' if isinstance(exc, TimeoutError) else 'failed'
			if on_attempt_finished is not None:
				on_attempt_finished(attempt, status, time.monotonic() - started_at, exc)
			if not is_retryable_model_error(exc) or attempt == MODEL_RETRY_MAX_ATTEMPTS:
				raise
			# A dynamic deadline (the Agent's shared task deadline) must leave a
			# full retry interval and a non-zero next attempt budget.
			if callable(timeout_seconds) and timeout_seconds() <= MODEL_RETRY_DELAY_SECONDS:
				raise
			retry_started_at = time.monotonic()
			try:
				await asyncio.sleep(MODEL_RETRY_DELAY_SECONDS)
			except asyncio.CancelledError:
				if on_retry_wait_finished is not None:
					on_retry_wait_finished(attempt, time.monotonic() - retry_started_at)
				raise
			if on_retry_wait_finished is not None:
				on_retry_wait_finished(attempt, time.monotonic() - retry_started_at)
		else:
			if on_attempt_finished is not None:
				on_attempt_finished(attempt, 'successful', time.monotonic() - started_at, None)
			return result

	raise AssertionError('model retry loop exited without returning or raising')


__all__ = [
	'MODEL_RETRY_DELAY_SECONDS',
	'MODEL_RETRY_MAX_ATTEMPTS',
	'await_with_hard_timeout',
	'invoke_with_reconnect_retries',
	'is_retryable_model_error',
]
