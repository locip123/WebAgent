from __future__ import annotations

import asyncio
import base64
import json
import re
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
from browser_use.webretriever.network import ChartNetworkInspector
from browser_use.webretriever.prompts import (
	DEFAULT_THOUGHT_LANGUAGE,
	build_step_prompt,
	build_system_prompt,
	normalize_thought_language,
)

T = TypeVar('T')
_FIND_CHART_MAX_SECONDS = 60.0
_ANALYSIS_MAX_SECONDS = 90.0
_FINISH_RESERVE_SECONDS = 30.0


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
		chart_network_inspector: Any | None = None,
		data_analysis_assistant: Any | None = None,
		task_deadline_monotonic: float | None = None,
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
		self.task_deadline_monotonic = task_deadline_monotonic
		self._trusted_chart_manifests: dict[str, str] = {}
		self._ready_chart_data_dirs: set[str] = set()
		self._chart_artifact_filters: dict[str, dict[str, Any]] = {}
		self.chart_network_inspector = chart_network_inspector or ChartNetworkInspector(
			llm,
			# Leave room inside the 300-second task watchdog for normalization,
			# the 90-second analysis action, and the final grounded answer.
			model_timeout_seconds=min(_FIND_CHART_MAX_SECONDS, model_timeout_seconds),
		)
		# Import the optional PandasAI runtime only if the model actually selects
		# its action.  Ordinary browser tasks and unit tests therefore do not pay
		# its import/dependency cost.
		self.data_analysis_assistant = data_analysis_assistant
		# The runner can enforce a task-wide deadline while this coroutine is in
		# flight.  Retain the mutable outcome so it can persist all completed work
		# if that outer deadline cancels ``run`` before it returns.
		self._partial_outcome: AgentRunOutcome | None = None

	@property
	def partial_outcome(self) -> AgentRunOutcome | None:
		"""Actions, thoughts, and steps completed before an external cancellation."""

		return self._partial_outcome

	def _get_data_analysis_assistant(self) -> Any:
		if self.data_analysis_assistant is None:
			from browser_use.webretriever.data_analysis import DataAnalysisAssistant

			self.data_analysis_assistant = DataAnalysisAssistant(
				self.llm,
				task_dir=self.task_dir,
				task_identity=self.task.prompt_payload(),
				model_timeout_seconds=min(90.0, self.model_timeout_seconds),
				max_output_chars=32_000,
				trusted_manifest_hashes=self._trusted_chart_manifests,
			)
		return self.data_analysis_assistant

	def _remaining_task_seconds(self) -> float:
		if self.task_deadline_monotonic is None:
			return float('inf')
		return max(0.0, self.task_deadline_monotonic - time.monotonic())

	def _chart_action_budget(self, action: str, *, cursor: bool = False) -> float:
		remaining = self._remaining_task_seconds()
		if action == 'find_chart_data_requests':
			if cursor:
				return max(0.0, min(5.0, remaining - _FINISH_RESERVE_SECONDS))
			# A find call must leave a full analysis window and one final model turn.
			return max(0.0, min(_FIND_CHART_MAX_SECONDS, remaining - _ANALYSIS_MAX_SECONDS - _FINISH_RESERVE_SECONDS))
		return max(0.0, min(_ANALYSIS_MAX_SECONDS, remaining - _FINISH_RESERVE_SECONDS))

	def _register_chart_artifact(self, output: str) -> dict[str, Any] | None:
		try:
			payload = json.loads(output)
		except (TypeError, json.JSONDecodeError):
			return None
		if not isinstance(payload, dict) or payload.get('status') != 'ready':
			return payload if isinstance(payload, dict) else None
		data_dir = payload.get('data_dir')
		manifest_sha256 = payload.get('manifest_sha256')
		if not isinstance(data_dir, str) or not isinstance(manifest_sha256, str):
			return payload
		try:
			resolved = Path(data_dir).resolve(strict=True)
			chart_root = (self.task_dir.resolve(strict=True) / 'chart_data').resolve(strict=True)
		except (FileNotFoundError, OSError):
			return payload
		if resolved == chart_root or not resolved.is_relative_to(chart_root):
			return payload
		key = str(resolved)
		self._trusted_chart_manifests[key] = manifest_sha256
		self._ready_chart_data_dirs.add(key)
		active_filters = payload.get('active_filters')
		if not isinstance(active_filters, dict):
			datasets = payload.get('datasets')
			if isinstance(datasets, list):
				candidates = [item.get('active_filters') for item in datasets if isinstance(item, dict)]
				if candidates and isinstance(candidates[0], dict) and all(item == candidates[0] for item in candidates):
					active_filters = candidates[0]
		self._chart_artifact_filters[key] = dict(active_filters) if isinstance(active_filters, dict) else {}
		return payload

	def _is_ready_chart_data_dir(self, data_dir: str | None) -> bool:
		if not isinstance(data_dir, str):
			return False
		try:
			return str(Path(data_dir).resolve(strict=True)) in self._ready_chart_data_dirs
		except (FileNotFoundError, OSError):
			return False

	def _chart_filter_mismatch(self, data_dir: str | None, analysis_query: str | None) -> str | None:
		if not isinstance(data_dir, str):
			return None
		try:
			filters = self._chart_artifact_filters.get(str(Path(data_dir).resolve(strict=True)), {})
		except (FileNotFoundError, OSError):
			return None
		requested_years = set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', f'{self.task.task}\n{analysis_query or ""}'))
		if len(requested_years) != 1:
			return None
		active_years: set[str] = set()
		for name, value in filters.items():
			folded = str(name).casefold()
			if 'year' not in folded and not any(marker in str(name) for marker in ('年份', '年度', '年')):
				continue
			active_years.update(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', json.dumps(value, ensure_ascii=False)))
		if active_years and active_years != requested_years:
			return (
				f'active Year filter {sorted(active_years)} conflicts with requested year {sorted(requested_years)}; '
				'correct the visible UI and run find_chart_data_requests again'
			)
		return None

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

			model_call_timeout = min(self.model_timeout_seconds, self._remaining_task_seconds())
			if model_call_timeout <= 0:
				outcome.status = 'FAIL_TASK_TIMEOUT'
				outcome.error = 'Task deadline elapsed before the next model decision'
				break
			try:
				response = await _await_with_hard_timeout(
					self.llm.ainvoke(messages, output_format=AgentDecision),
					model_call_timeout,
				)
				decision = response.completion
				_merge_usage(outcome.usage, _usage_dict(response.usage))
			except TimeoutError:
				model_error = f'Model request exceeded {model_call_timeout:g} seconds'
				_save_visual_screenshot(
					screenshot,
					self.task_dir / 'trajectory_visual' / f'{step}.png',
					model_error,
					observation,
				)
				consecutive_model_timeouts += 1
				consecutive_model_output_errors = 0
				last_outcome = (
					f'ERROR: The prior model request exceeded {model_call_timeout:g} seconds; '
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
				if decision.action == 'find_chart_data_requests':
					budget = self._chart_action_budget(decision.action, cursor=decision.cursor is not None)
					if budget <= 0:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'timeout',
								'error': (
									'insufficient task time remains for cursor inspection and finish'
									if decision.cursor is not None
									else 'insufficient task time remains for find plus analysis and finish'
								),
							},
							separators=(',', ':'),
						)
						action_failed = True
					else:
						execution = await _await_with_hard_timeout(
							self.chart_network_inspector.execute(
								runtime=self.runtime,
								task=self.task.task,
								page_url=observation.url,
								page_title=getattr(observation, 'title', ''),
								cursor=decision.cursor,
								task_dir=self.task_dir,
								task_identity=self.task.prompt_payload(),
							),
							budget,
						)
						last_outcome = execution.output
						_merge_usage(outcome.usage, execution.usage)
						payload = self._register_chart_artifact(last_outcome)
						if decision.cursor is not None:
							action_failed = not isinstance(payload, dict) or payload.get('status') in {'stale_state', 'timeout'}
						else:
							action_failed = not isinstance(payload, dict) or payload.get('status') != 'ready'
				elif decision.action == 'call_data_analysis_assistant':
					budget = self._chart_action_budget(decision.action)
					filter_mismatch = self._chart_filter_mismatch(decision.data_dir, decision.analysis_query)
					if not self._is_ready_chart_data_dir(decision.data_dir):
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'invalid_data_dir',
								'error': 'data_dir must be the exact ready directory returned by find_chart_data_requests in this task run',
							},
							separators=(',', ':'),
						)
						action_failed = True
					elif filter_mismatch is not None:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'invalid_manifest',
								'error': filter_mismatch,
							},
							ensure_ascii=False,
							separators=(',', ':'),
						)
						action_failed = True
					elif budget <= 0:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'timeout',
								'error': 'insufficient task time remains for analysis and finish',
							},
							separators=(',', ':'),
						)
						action_failed = True
					else:
						execution = await _await_with_hard_timeout(
							self._get_data_analysis_assistant().execute(
								analysis_query=decision.analysis_query,
								data_dir=decision.data_dir,
							),
							budget,
						)
						last_outcome = execution.output
						_merge_usage(outcome.usage, execution.usage)
						try:
							payload = json.loads(last_outcome)
						except (TypeError, json.JSONDecodeError):
							payload = None
						action_failed = not isinstance(payload, dict) or payload.get('status') != 'ok'
				else:
					last_outcome = await self.runtime.execute(decision)
			except TimeoutError:
				last_outcome = json.dumps(
					{'action': decision.action, 'status': 'timeout', 'error': 'action exceeded its shared task budget'},
					separators=(',', ':'),
				)
				action_failed = True
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
