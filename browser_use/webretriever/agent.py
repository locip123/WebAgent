from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar

from PIL import Image, ImageDraw, ImageFont

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, SystemMessage, UserMessage
from browser_use.webretriever.models import AgentDecision, CompetitionTask
from browser_use.webretriever.prompts import (
	DEFAULT_THOUGHT_LANGUAGE,
	build_step_prompt,
	build_system_prompt,
	normalize_thought_language,
)

T = TypeVar('T')


@dataclass(slots=True)
class AgentRunOutcome:
	status: str
	agent_answer: str = ''
	evidence: list[str] = field(default_factory=list)
	actions: list[str] = field(default_factory=list)
	thoughts: list[str] = field(default_factory=list)
	steps: list[dict[str, Any]] = field(default_factory=list)
	error: str | None = None
	duration_seconds: float = 0.0
	usage: dict[str, int] = field(default_factory=dict)


def _decision_action_payload(decision: AgentDecision) -> dict[str, Any]:
	payload = decision.model_dump(exclude_none=True, mode='json')
	for field_name in ('thought', 'memory', 'answer', 'evidence', 'success'):
		if decision.action != 'finish' or field_name in ('thought', 'memory'):
			payload.pop(field_name, None)
	return payload


def _action_string(decision: AgentDecision) -> str:
	return json.dumps(_decision_action_payload(decision), ensure_ascii=False, separators=(',', ':'))


def _usage_dict(usage: Any) -> dict[str, int]:
	if usage is None:
		return {}
	if hasattr(usage, 'model_dump'):
		raw = usage.model_dump(exclude_none=True)
	else:
		raw = vars(usage) if hasattr(usage, '__dict__') else {}
	return {str(key): int(value) for key, value in raw.items() if isinstance(value, int)}


def _merge_usage(total: dict[str, int], current: dict[str, int]) -> None:
	for key, value in current.items():
		total[key] = total.get(key, 0) + value


def _consume_detached_task_result(task: asyncio.Future[Any]) -> None:
	"""Retrieve a late result so a cancellation-resistant task cannot warn."""
	if task.cancelled():
		return
	try:
		task.exception()
	except BaseException:
		pass


async def _await_with_hard_timeout(awaitable: Awaitable[T], timeout: float) -> T:
	"""Return at the deadline even when the awaited coroutine resists cancellation.

	``asyncio.wait_for`` cancels a timed-out child and then waits for that child
	to acknowledge cancellation.  Network stacks with lengthy cleanup (or a
	buggy compatibility proxy) can therefore defeat the apparent timeout.  A
	plain wait lets us issue cancellation without extending the caller's hard
	deadline.
	"""
	task = asyncio.ensure_future(awaitable)
	try:
		done, _ = await asyncio.wait({task}, timeout=timeout)
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


def _is_invalid_structured_output(exc: Exception) -> bool:
	"""Identify provider responses that can be retried within the step budget."""
	if not isinstance(exc, ModelProviderError):
		return False
	message = str(exc)
	return 'validation error for AgentDecision' in message or 'Failed to parse structured output' in message


def _element_box(observation: Any, element_id: int | None) -> tuple[float, float, float, float] | None:
	if element_id is None:
		return None
	for element in getattr(observation, 'elements', []):
		candidate_id = getattr(element, 'element_id', getattr(element, 'index', None))
		if candidate_id != element_id:
			continue
		box = getattr(element, 'box', None) or getattr(element, 'bbox', None)
		if isinstance(box, dict):
			return tuple(float(box.get(key, 0)) for key in ('x', 'y', 'width', 'height'))  # type: ignore[return-value]
		if isinstance(box, (list, tuple)) and len(box) >= 4:
			return tuple(float(value) for value in box[:4])  # type: ignore[return-value]
		values = tuple(getattr(element, key, None) for key in ('x', 'y', 'width', 'height'))
		if all(isinstance(value, (int, float)) for value in values):
			return values  # type: ignore[return-value]
	return None


def _save_visual_screenshot(
	image_bytes: bytes,
	path: Path,
	label: str,
	observation: Any,
	element_id: int | None = None,
) -> None:
	"""Save an action-labelled screenshot without modifying the live page."""
	image = Image.open(BytesIO(image_bytes)).convert('RGB')
	draw = ImageDraw.Draw(image)
	box = _element_box(observation, element_id)
	if box:
		x, y, width, height = box
		draw.rectangle((x, y, x + width, y + height), outline=(239, 68, 68), width=4)

	font = ImageFont.load_default()
	printable_label = label.encode('ascii', errors='replace').decode('ascii')[:180]
	text_box = draw.textbbox((8, 8), printable_label, font=font)
	draw.rectangle((4, 4, text_box[2] + 12, text_box[3] + 12), fill=(255, 255, 255), outline=(239, 68, 68))
	draw.text((8, 8), printable_label, fill=(185, 28, 28), font=font)
	path.parent.mkdir(parents=True, exist_ok=True)
	image.save(path, format='PNG')


class ProtocolIIIAgent:
	"""A one-action-per-step Protocol III agent over a Playwright runtime."""

	def __init__(
		self,
		*,
		task: CompetitionTask,
		llm: BaseChatModel,
		runtime: Any,
		task_dir: Path,
		max_steps: int = 100,
		model_timeout_seconds: float = 180.0,
		max_consecutive_action_errors: int = 5,
		max_consecutive_model_output_errors: int = 3,
		max_consecutive_model_timeouts: int = 2,
		thought_language: str = DEFAULT_THOUGHT_LANGUAGE,
	):
		if not 1 <= max_steps <= 100:
			raise ValueError('max_steps must be between 1 and the competition limit of 100')
		if not 0 < model_timeout_seconds <= 180:
			raise ValueError('model_timeout_seconds must be in (0, 180]')
		if max_consecutive_action_errors < 1:
			raise ValueError('max_consecutive_action_errors must be at least 1')
		if max_consecutive_model_output_errors < 1:
			raise ValueError('max_consecutive_model_output_errors must be at least 1')
		if max_consecutive_model_timeouts < 1:
			raise ValueError('max_consecutive_model_timeouts must be at least 1')
		self.task = task
		self.llm = llm
		self.runtime = runtime
		self.task_dir = Path(task_dir)
		self.max_steps = max_steps
		self.model_timeout_seconds = model_timeout_seconds
		self.max_consecutive_action_errors = max_consecutive_action_errors
		self.max_consecutive_model_output_errors = max_consecutive_model_output_errors
		self.max_consecutive_model_timeouts = max_consecutive_model_timeouts
		self.thought_language = normalize_thought_language(thought_language)
		self.system_prompt = build_system_prompt(self.thought_language)
		# The runner can enforce a task-wide deadline while this coroutine is in
		# flight.  Retain the mutable outcome so it can persist all completed work
		# if that outer deadline cancels ``run`` before it returns.
		self._partial_outcome: AgentRunOutcome | None = None

	@property
	def partial_outcome(self) -> AgentRunOutcome | None:
		"""Actions, thoughts, and steps completed before an external cancellation."""

		return self._partial_outcome

	async def run(self) -> AgentRunOutcome:
		started_at = time.monotonic()
		outcome = AgentRunOutcome(status='FAIL')
		self._partial_outcome = outcome
		memory = ''
		last_outcome = 'The task has just started.'
		consecutive_errors = 0
		consecutive_model_output_errors = 0
		consecutive_model_timeouts = 0

		for step in range(self.max_steps):
			try:
				observation = await self.runtime.observe(step)
			except Exception as exc:
				outcome.status = 'FAIL_BROWSER'
				outcome.error = f'Observation failed: {type(exc).__name__}: {exc}'
				break

			screenshot = observation.screenshot
			raw_path = self.task_dir / 'trajectory' / f'{step}.png'
			raw_path.parent.mkdir(parents=True, exist_ok=True)
			# BrowserRuntime writes the unmodified screenshot before adding element
			# overlays.  Fakes may not, so retain a small compatibility fallback.
			if not raw_path.exists():
				raw_path.write_bytes(screenshot)

			prompt = build_step_prompt(
				task=self.task.task,
				website=self.task.website,
				step=step,
				max_steps=self.max_steps,
				observation=observation.render_text(),
				history=outcome.steps[-12:],
				memory=memory,
				last_outcome=last_outcome,
			)
			messages = [
				SystemMessage(content=self.system_prompt),
				UserMessage(
					content=[
						ContentPartTextParam(text=prompt),
						ContentPartImageParam(
							image_url=ImageURL(
								url=f'data:image/png;base64,{base64.b64encode(screenshot).decode("ascii")}',
								detail='high',
							)
						),
					]
				),
			]

			try:
				response = await _await_with_hard_timeout(
					self.llm.ainvoke(messages, output_format=AgentDecision),
					self.model_timeout_seconds,
				)
				decision = response.completion
				_merge_usage(outcome.usage, _usage_dict(response.usage))
			except TimeoutError:
				model_error = f'Model request exceeded {self.model_timeout_seconds:g} seconds'
				_save_visual_screenshot(
					screenshot,
					self.task_dir / 'trajectory_visual' / f'{step}.png',
					model_error,
					observation,
				)
				consecutive_model_timeouts += 1
				consecutive_model_output_errors = 0
				last_outcome = (
					f'ERROR: The prior model request exceeded {self.model_timeout_seconds:g} seconds; '
					'no browser action was executed. Reassess the unchanged page and return one concise action.'
				)
				outcome.steps.append(
					{
						'step': step,
						'url': observation.url,
						'thought': '',
						'action': {},
						'outcome': last_outcome,
					}
				)
				if consecutive_model_timeouts < self.max_consecutive_model_timeouts:
					continue
				outcome.status = 'FAIL_MODEL_TIMEOUT'
				outcome.error = model_error
				break
			except Exception as exc:
				model_error = f'Model request failed: {type(exc).__name__}: {exc}'
				_save_visual_screenshot(
					screenshot,
					self.task_dir / 'trajectory_visual' / f'{step}.png',
					model_error,
					observation,
				)
				if _is_invalid_structured_output(exc):
					consecutive_model_output_errors += 1
					consecutive_model_timeouts = 0
					last_outcome = (
						'ERROR: The prior model response was not a valid AgentDecision, so no browser action was executed. '
						'Return exactly one complete schema-constrained action now. '
						f'Diagnostic: {str(exc)[:1000]}'
					)
					outcome.steps.append(
						{
							'step': step,
							'url': observation.url,
							'thought': '',
							'action': {},
							'outcome': last_outcome,
						}
					)
					if consecutive_model_output_errors < self.max_consecutive_model_output_errors:
						continue
				outcome.status = 'FAIL_MODEL'
				outcome.error = model_error
				break

			consecutive_model_output_errors = 0
			consecutive_model_timeouts = 0
			action_text = _action_string(decision)
			_save_visual_screenshot(
				screenshot,
				self.task_dir / 'trajectory_visual' / f'{step}.png',
				action_text,
				observation,
				decision.element_id,
			)
			outcome.actions.append(action_text)
			outcome.thoughts.append(decision.thought)

			step_record: dict[str, Any] = {
				'step': step,
				'url': observation.url,
				'thought': decision.thought,
				'action': _decision_action_payload(decision),
			}

			if decision.action == 'finish':
				answer = (decision.answer or '').strip()
				evidence = [item.strip() for item in (decision.evidence or []) if item.strip()]
				if decision.success is True and answer and evidence:
					step_record['outcome'] = 'Task completed with browser-grounded evidence.'
					outcome.steps.append(step_record)
					outcome.status = 'SUCCESS'
					outcome.agent_answer = answer
					outcome.evidence = evidence
					break
				if decision.success is False:
					step_record['outcome'] = 'Agent declared the task unsuccessful.'
					outcome.steps.append(step_record)
					outcome.status = 'FAIL'
					outcome.error = answer or 'Agent could not complete the task'
					break
				last_outcome = 'ERROR: finish(success=true) requires a non-empty answer and at least one evidence item.'
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				continue

			# Persist the initiated action before awaiting the browser.  A task-wide
			# watchdog may cancel a slow browser operation, but its action, thought,
			# and step still belong in the final timeout artifact.
			step_record['outcome'] = 'Action started; browser result was not recorded yet.'
			outcome.steps.append(step_record)
			action_failed = False
			try:
				last_outcome = await self.runtime.execute(decision)
			except Exception as exc:
				last_outcome = f'ERROR: {type(exc).__name__}: {exc}'
				action_failed = True

			step_record['outcome'] = (
				last_outcome
				if len(last_outcome) <= 20_000
				else f'{last_outcome[:14_000]}\n...[action result truncated]...\n{last_outcome[-6_000:]}'
			)
			memory = (
				decision.memory
				if len(decision.memory) <= 6000
				else f'{decision.memory[:4000]}\n...[memory bounded]...\n{decision.memory[-1950:]}'
			)
			if action_failed:
				consecutive_errors += 1
			else:
				consecutive_errors = 0

			if consecutive_errors >= self.max_consecutive_action_errors:
				outcome.status = 'FAIL_ACTIONS'
				outcome.error = f'{consecutive_errors} consecutive browser actions failed; last error: {last_outcome}'
				break
		else:
			outcome.status = 'FAIL_MAX_STEPS'
			outcome.error = f'Reached the competition limit of {self.max_steps} steps without a grounded final answer'

		outcome.duration_seconds = round(time.monotonic() - started_at, 3)
		return outcome
