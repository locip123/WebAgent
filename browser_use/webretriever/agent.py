from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar

from PIL import Image, ImageDraw, ImageFont

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, SystemMessage, UserMessage
from browser_use.webretriever.artifacts import (
	MODEL_PROMPT_LOG_FILENAME,
	MODEL_PROMPT_LOG_FORMAT,
	STRATEGY_REVIEW_PROMPT_LOG_FILENAME,
	STRATEGY_REVIEW_PROMPT_LOG_FORMAT,
	STRUCTURED_MODEL_PROMPT_LOG_FORMAT,
	atomic_write_json,
	model_prompt_log_metadata,
	prompt_text_lines,
)
from browser_use.webretriever.models import AgentDecision, CompetitionTask
from browser_use.webretriever.network import ChartNetworkInspector
from browser_use.webretriever.prompts import (
	DEFAULT_THOUGHT_LANGUAGE,
	PromptComposer,
	PromptDocument,
	PromptError,
	PromptTarget,
	StepContext,
	normalize_thought_language,
)
from browser_use.webretriever.strategy import (
	ExplorationCheckpointTracker,
	StrategyCheckpointError,
	StrategyReviewRequest,
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
	payload = decision.action_payload()
	for field_name in ('thought', 'memory', 'answer', 'evidence', 'success'):
		if decision.action != 'finish' or field_name in ('thought', 'memory'):
			payload.pop(field_name, None)
	return payload


def _action_string(decision: AgentDecision) -> str:
	return json.dumps(_decision_action_payload(decision), ensure_ascii=False, separators=(',', ':'))


def _observation_hash(rendered_observation: str) -> str:
	"""Stable browser-state identity used only for exact-action loop protection."""

	return hashlib.sha256(rendered_observation.encode('utf-8')).hexdigest()


# Actions whose whole purpose is to change the viewport or the browsing position.
# Repeating them with different parameters is normal exploration; alternating
# between two of them over the same states is a stall.
_NAVIGATION_ACTIONS = frozenset({'scroll', 'back', 'navigate', 'click_xy', 'switch_tab', 'drag'})
# Read-only probes: many of these in a row without any state change means the
# current modality is exhausted, no matter how the query string varies.
_PROBE_ACTIONS = frozenset({'inspect_network', 'find_text', 'read_element', 'find_chart_data_requests'})


def _action_intent(decision: AgentDecision) -> str:
	"""Coarse action identity that ignores incidental parameter churn.

	Case 62 re-submitted the same form through five different ``element_id``
	values, case 77 issued 48 ``inspect_network`` calls with 48 different query
	strings, and case 95 dragged the same date-picker column ten times with
	endpoints jittering by a handful of pixels.  Each is one intent repeated, so
	loop detection keys on the action plus only the parameters that change *what*
	is being attempted -- never the coordinates or element handle used to reach it.
	"""

	if decision.action == 'scroll':
		return f'scroll:{decision.direction}'
	if decision.action in _PROBE_ACTIONS:
		return decision.action
	payload = _decision_action_payload(decision)
	for incidental in ('element_id', 'x', 'y', 'end_x', 'end_y'):
		payload.pop(incidental, None)
	return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def _detect_loop(
	recent: Sequence[tuple[str, str]],
	*,
	oscillation_cycles: int = 3,
	probe_family_limit: int = 6,
	churn_span: int = 12,
	churn_max_intents: int = 3,
	churn_max_states: int = 2,
) -> dict[str, Any] | None:
	"""Classify a stalled trajectory from recent ``(intent, observation)`` pairs.

	Returns ``None`` while progress looks plausible.  The exact-repeat guard this
	supplements only fired on byte-identical observations, so every failure case
	that alternated actions or varied one parameter escaped it entirely.
	"""

	recent = list(recent)
	if not recent:
		return None
	intents = [intent for intent, _ in recent]
	current = intents[-1]

	# A<->B oscillation: two intents alternating over a small set of states.
	window = 2 * oscillation_cycles
	if len(intents) >= window:
		tail = intents[-window:]
		first, second = tail[0], tail[1]
		if first != second and all(tail[index] == (first if index % 2 == 0 else second) for index in range(window)):
			if first in _NAVIGATION_ACTIONS or second in _NAVIGATION_ACTIONS or first.startswith('scroll:'):
				states = {state for _, state in recent[-window:]}
				if len(states) <= oscillation_cycles:
					return {
						'pattern': 'oscillation',
						'cycles': oscillation_cycles,
						'alternating_intents': [first, second],
						'distinct_states': len(states),
					}

	# Unproductive probe family: the same read-only action repeated with only
	# parameter changes, while the observed page state never advances.
	if current in _PROBE_ACTIONS:
		streak = 0
		for intent, _ in reversed(recent):
			if intent != current:
				break
			streak += 1
		if streak >= probe_family_limit:
			return {
				'pattern': 'unproductive_probe_family',
				'attempts': streak,
				'distinct_states': len({state for _, state in recent[-streak:]}),
			}

	# Intent churn: a long stretch cycling among a handful of intents while the
	# observed state barely moves.  Case 95 spent 34 steps rotating click/drag/type
	# over one date picker and case 62 rotated find_text/click/scroll over one form;
	# neither alternates strictly enough for the pairwise check above to see it.
	if len(recent) >= churn_span:
		tail = recent[-churn_span:]
		tail_intents = {intent for intent, _ in tail}
		tail_states = {state for _, state in tail}
		if len(tail_intents) <= churn_max_intents and len(tail_states) <= churn_max_states:
			return {
				'pattern': 'intent_churn',
				'span': churn_span,
				'distinct_intents': len(tail_intents),
				'distinct_states': len(tail_states),
			}
	return None


def _salvage_answer(memory: str, steps: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]] | None:
	"""Recover verified partial findings when the task deadline arrives.

	Seven timeout artifacts returned ``agent_answer: ''`` while their memory
	ledgers still held concrete verified values.  A partial, clearly-labelled
	answer beats a blank one, so the durable ``Verified:`` section is promoted
	into the outcome instead of being discarded.
	"""

	verified: list[str] = []
	capturing = False
	for raw_line in memory.splitlines():
		line = raw_line.strip()
		if not line:
			continue
		lowered = line.lower()
		if lowered.startswith('verified'):
			capturing = True
			remainder = line.split(':', 1)[1].strip() if ':' in line else ''
			if remainder:
				verified.append(remainder)
			continue
		if capturing and any(
			lowered.startswith(heading) for heading in ('constraints', 'candidates', 'tried-blocked', 'tried', 'next')
		):
			capturing = False
			continue
		if capturing:
			verified.append(line.lstrip('-* ').strip())
	if not verified:
		return None
	answer = ' '.join(verified).strip()
	if not answer:
		return None
	last_url = ''
	for record in reversed(steps):
		candidate = str(record.get('url', '') or '')
		if candidate:
			last_url = candidate
			break
	evidence = [
		f'Partial result salvaged at the task deadline from browser-verified findings: {answer}'
		+ (f' (last observed page: {last_url})' if last_url else '')
	]
	return answer, evidence


def _bounded_memory(value: str) -> str:
	"""Keep the durable replacement ledger within the prompt contract."""

	if len(value) <= 3_000:
		return value
	marker = '\n...[memory bounded to 3,000 characters]...\n'
	head = 2_000
	return value[:head] + marker + value[-(3_000 - head - len(marker)) :]


def _compact_checkpoint_observation(rendered_observation: str, *, limit: int = 900) -> str:
	"""Retain enough prior-page context for one adaptive strategy review."""

	normalized = re.sub(r'\s+', ' ', rendered_observation).strip()
	if len(normalized) <= limit:
		return normalized
	marker = ' ...[browser observation compacted]... '
	head = (limit * 2) // 3
	tail = max(0, limit - head - len(marker))
	return normalized[:head] + marker + (normalized[-tail:] if tail else '')


def _checkpoint_trajectory_record(
	*,
	step: int,
	observation: Any,
	rendered_observation: str,
	decision: AgentDecision,
	outcome: str,
) -> dict[str, Any]:
	"""Build the browser-derived record consumed only by a future checkpoint."""

	return {
		'step': step + 1,
		'url': str(getattr(observation, 'url', '')),
		'title': str(getattr(observation, 'title', '')),
		'page_observation': _compact_checkpoint_observation(rendered_observation),
		'action': _decision_action_payload(decision),
		'outcome': _compact_checkpoint_observation(outcome, limit=1_200),
	}


def _record_exploration_decision(
	tracker: ExplorationCheckpointTracker,
	*,
	step: int,
	observation: Any,
	rendered_observation: str,
	decision: AgentDecision,
	outcome: str,
) -> None:
	"""Count every completed valid decision, including failed or locally blocked ones."""

	tracker.record_decision(
		_checkpoint_trajectory_record(
			step=step,
			observation=observation,
			rendered_observation=rendered_observation,
			decision=decision,
			outcome=outcome,
		)
	)


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
		structured_prompt_log: bool = False,
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
		self.structured_prompt_log = structured_prompt_log
		model_id = getattr(llm, 'model', None)
		self.prompt_composer = PromptComposer(
			self.task,
			PromptTarget(model_id=str(model_id) if model_id else 'gpt-5.4'),
			max_steps=self.max_steps,
			thought_language=self.thought_language,
		)
		self.system_document = self.prompt_composer.system
		self.system_prompt = self.system_document.text
		self._model_prompt_log_path = self.task_dir / MODEL_PROMPT_LOG_FILENAME
		self._model_prompt_log: dict[str, Any] = {}
		self._strategy_review_prompt_log_path = self.task_dir / STRATEGY_REVIEW_PROMPT_LOG_FILENAME
		self._strategy_review_prompt_log: dict[str, Any] = {}
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
		# Latest durable ledger, kept outside the loop so a cancelled run can
		# still be salvaged by the runner's task watchdog.
		self._last_memory: str = ''

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

	def _remember(self, value: str) -> str:
		"""Bound the durable ledger and retain it for deadline salvage."""

		self._last_memory = _bounded_memory(value)
		return self._last_memory

	def salvage_partial_answer(self) -> bool:
		"""Promote verified memory findings into the partial outcome.

		Called by the agent loop on a deadline exit and by the runner when the
		task watchdog cancels the loop mid-action.  Never overwrites an answer
		the model already produced.
		"""

		outcome = self._partial_outcome
		if outcome is None:
			return False
		return self._salvage_into(outcome, self._last_memory)

	def _salvage_into(self, outcome: AgentRunOutcome, memory: str) -> bool:
		if outcome.agent_answer:
			return False
		salvaged = _salvage_answer(memory, outcome.steps)
		if salvaged is None:
			return False
		outcome.agent_answer, outcome.evidence = salvaged
		return True

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

	def _reset_model_prompt_log(self) -> None:
		"""Start a fresh, durable prompt log in the configured display format."""

		self._reset_strategy_review_prompt_log()
		if not self.structured_prompt_log:
			self._model_prompt_log = {
				'format': MODEL_PROMPT_LOG_FORMAT,
				'system_prompt': prompt_text_lines(self.system_prompt),
				'steps': [],
			}
			atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)
			return

		self._model_prompt_log = {
			'format': STRUCTURED_MODEL_PROMPT_LOG_FORMAT,
			'metadata': model_prompt_log_metadata(),
			'task': self.task.prompt_payload(),
			# The system message is identical for every step, so storing it once
			# avoids duplicating a large prompt while retaining the complete input.
			'system_prompt': {
				'role': 'system',
				'rendered_text': self.system_document.text,
				'sections': [dict(section) for section in self.system_document.sections],
				'metrics': dict(self.system_document.metrics),
			},
			'steps': [],
		}
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)

	def _reset_strategy_review_prompt_log(self) -> None:
		"""Start the independent, complete input trace for strategy-review calls."""

		self._strategy_review_prompt_log = {
			'format': STRATEGY_REVIEW_PROMPT_LOG_FORMAT,
			# As in model_prompts.json, retain the shared system message once while
			# every review entry retains its exact user text and screenshot reference.
			'system_prompt': prompt_text_lines(self.system_prompt),
			'reviews': [],
		}
		atomic_write_json(self._strategy_review_prompt_log_path, self._strategy_review_prompt_log)

	def _record_model_prompt(
		self,
		*,
		step: int,
		prompt_document: PromptDocument,
		screenshot_path: Path,
	) -> None:
		"""Atomically persist one model request before it is submitted.

		The screenshot is already retained in ``trajectory``.  Referencing that
		file keeps the JSON readable while preserving the exact image bytes that
		were encoded into the multimodal request.  The default line-oriented log
		is intentionally simple; the detailed trace is opt-in for debug UIs.
		"""

		steps = self._model_prompt_log['steps']
		if not isinstance(steps, list):  # Defensive guard for future format edits.
			raise TypeError('model prompt log steps must be a list')
		image = {
			'media_type': 'image/png',
			'detail': 'high',
			'path': str(screenshot_path.relative_to(self.task_dir)),
		}
		if not self.structured_prompt_log:
			steps.append({'step': step + 1, 'prompt': prompt_text_lines(prompt_document.text), 'image': image})
		else:
			steps.append(
				{
					'step': step + 1,
					'prompt': {
						'role': prompt_document.role,
						'rendered_text': prompt_document.text,
						'sections': [dict(section) for section in prompt_document.sections],
						'metrics': dict(prompt_document.metrics),
					},
					'image': image,
				}
			)
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)

	def _record_strategy_review_prompt(
		self,
		*,
		step: int,
		strategy_review: StrategyReviewRequest,
		prompt_document: PromptDocument,
		screenshot_path: Path,
	) -> None:
		"""Persist the complete input of each initial, page-entry, or periodic review.

		The regular ``model_prompts.json`` contains every decision call.  This
		separate file lets investigators inspect only strategy-review calls without
		reconstructing them from the broader trajectory.
		"""

		reviews = self._strategy_review_prompt_log['reviews']
		if not isinstance(reviews, list):  # Defensive guard for future format edits.
			raise TypeError('strategy review prompt log reviews must be a list')
		reviews.append(
			{
				'step': step + 1,
				'trigger': strategy_review.trigger,
				'completed_decisions': strategy_review.completed_decisions,
				'trajectory_decision_count': len(strategy_review.trajectory),
				'prompt': prompt_text_lines(prompt_document.text),
				'image': {
					'media_type': 'image/png',
					'detail': 'high',
					'path': str(screenshot_path.relative_to(self.task_dir)),
				},
			}
		)
		atomic_write_json(self._strategy_review_prompt_log_path, self._strategy_review_prompt_log)

	def _record_model_result(
		self,
		*,
		step: int,
		started_at: float,
		usage: Mapping[str, int] | None = None,
		error: str | None = None,
	) -> None:
		"""Add call results after the already-durable structured model input."""

		if not self.structured_prompt_log:
			return
		steps = self._model_prompt_log.get('steps')
		if not isinstance(steps, list) or step >= len(steps):
			raise ValueError('model prompt result has no matching persisted input')
		entry = steps[step]
		if not isinstance(entry, dict):
			raise TypeError('structured model prompt step must be an object')
		entry['model_call'] = {
			'duration_seconds': round(max(0.0, time.monotonic() - started_at), 3),
			'usage': dict(usage or {}),
			'error': error,
		}
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)

	async def run(self) -> AgentRunOutcome:
		started_at = time.monotonic()
		outcome = AgentRunOutcome(status='FAIL')
		self._partial_outcome = outcome
		self._reset_model_prompt_log()
		memory = ''
		last_outcome = 'The task has just started.'
		consecutive_errors = 0
		consecutive_model_output_errors = 0
		consecutive_model_timeouts = 0
		last_action_signature: tuple[str, str] | None = None
		repeated_action_count = 0
		# Rolling (intent, observation) window that survives parameter churn and
		# action alternation, which the exact-repeat guard below cannot see.
		recent_signatures: deque[tuple[str, str]] = deque(maxlen=16)
		blocked_loop_intents: set[str] = set()
		exploration_tracker = ExplorationCheckpointTracker()

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

			rendered_observation = observation.render_text()
			observation_fingerprint = _observation_hash(rendered_observation)
			remaining = self._remaining_task_seconds()
			remaining_task_seconds = None if remaining == float('inf') else remaining
			strategy_review = exploration_tracker.review_request(current_page_url=observation.url)
			try:
				prompt_document = self.prompt_composer.compose_step(
					StepContext(
						step_index=step,
						observation=observation,
						history=tuple(outcome.steps),
						memory=memory,
						last_outcome=last_outcome,
						remaining_task_seconds=remaining_task_seconds,
						strategy_checkpoint=exploration_tracker.checkpoint,
						strategy_review=strategy_review,
					)
				)
			except PromptError as exc:
				outcome.status = 'FAIL_PROMPT'
				outcome.error = f'Prompt composition failed: {type(exc).__name__}: {exc}'
				break
			prompt = prompt_document.text
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
				self._salvage_into(outcome, memory)
				break
			self._record_model_prompt(
				step=step,
				prompt_document=prompt_document,
				screenshot_path=raw_path,
			)
			if strategy_review is not None:
				self._record_strategy_review_prompt(
					step=step,
					strategy_review=strategy_review,
					prompt_document=prompt_document,
					screenshot_path=raw_path,
				)
			model_call_started_at = time.monotonic()
			try:
				response = await _await_with_hard_timeout(
					self.llm.ainvoke(messages, output_format=AgentDecision),
					model_call_timeout,
				)
				decision = response.completion
				model_usage = _usage_dict(response.usage)
				_merge_usage(outcome.usage, model_usage)
				self._record_model_result(step=step, started_at=model_call_started_at, usage=model_usage)
			except TimeoutError:
				model_error = f'Model request exceeded {model_call_timeout:g} seconds'
				self._record_model_result(step=step, started_at=model_call_started_at, error=model_error)
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
				self._record_model_result(step=step, started_at=model_call_started_at, error=model_error)
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

			if strategy_review is not None:
				try:
					exploration_tracker.accept_review(
						strategy_catalog=decision.checkpoint_strategy_catalog,
						active_strategy=decision.checkpoint_active_strategy,
						confirmed_infeasible=decision.checkpoint_confirmed_infeasible,
						next_strategies=decision.checkpoint_next_strategies,
					)
				except StrategyCheckpointError as exc:
					model_error = f'Model response omitted or invalidated the required strategy checkpoint: {exc}'
					self._record_model_result(
						step=step,
						started_at=model_call_started_at,
						usage=model_usage,
						error=model_error,
					)
					_save_visual_screenshot(
						screenshot,
						self.task_dir / 'trajectory_visual' / f'{step}.png',
						model_error,
						observation,
					)
					consecutive_model_output_errors += 1
					consecutive_model_timeouts = 0
					last_outcome = (
						'ERROR: The prior model response did not include one complete required strategy checkpoint, so no browser '
						'action was executed. Return all four non-empty checkpoint_* fields and one normal action. '
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
					_record_exploration_decision(
						exploration_tracker,
						step=step,
						observation=observation,
						rendered_observation=rendered_observation,
						decision=decision,
						outcome=step_record['outcome'],
					)
					outcome.status = 'SUCCESS'
					outcome.agent_answer = answer
					outcome.evidence = evidence
					break
				if decision.success is False:
					step_record['outcome'] = 'Agent declared the task unsuccessful.'
					outcome.steps.append(step_record)
					_record_exploration_decision(
						exploration_tracker,
						step=step,
						observation=observation,
						rendered_observation=rendered_observation,
						decision=decision,
						outcome=step_record['outcome'],
					)
					outcome.status = 'FAIL'
					outcome.error = answer or 'Agent could not complete the task'
					break
				last_outcome = 'ERROR: finish(success=true) requires a non-empty answer and at least one evidence item.'
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				_record_exploration_decision(
					exploration_tracker,
					step=step,
					observation=observation,
					rendered_observation=rendered_observation,
					decision=decision,
					outcome=last_outcome,
				)
				continue

			action_signature = (observation_fingerprint, action_text)
			if action_signature == last_action_signature:
				repeated_action_count += 1
			else:
				last_action_signature = action_signature
				repeated_action_count = 1
			if repeated_action_count >= 3:
				last_outcome = json.dumps(
					{
						'action': decision.action,
						'status': 'repeated_unchanged_action',
						'repeat_count': repeated_action_count,
						'error': 'identical action was not executed because the browser observation did not change',
					},
					ensure_ascii=False,
					separators=(',', ':'),
				)
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				_record_exploration_decision(
					exploration_tracker,
					step=step,
					observation=observation,
					rendered_observation=rendered_observation,
					decision=decision,
					outcome=last_outcome,
				)
				memory = self._remember(decision.memory)
				consecutive_errors += 1
				if consecutive_errors >= self.max_consecutive_action_errors:
					outcome.status = 'FAIL_ACTIONS'
					outcome.error = f'{consecutive_errors} consecutive browser actions failed; last error: {last_outcome}'
					break
				continue

			action_intent = _action_intent(decision)
			recent_signatures.append((action_intent, observation_fingerprint))
			loop_report = _detect_loop(recent_signatures)
			if loop_report is not None:
				blocked_loop_intents.add(action_intent)
				last_outcome = json.dumps(
					{
						'action': decision.action,
						'status': 'loop_detected',
						**loop_report,
						'error': ('this action was not executed because the recent trajectory is repeating without progress'),
						'required_change': (
							'abandon this approach; switch modality or target. If page text and network capture both '
							'failed on the same value, read it visually from the screenshot at full resolution, open the '
							'first-party export/download, or navigate to a different source page. If the remaining task '
							'time is short, finish with the findings already verified in memory.'
						),
						'blocked_intents': sorted(blocked_loop_intents),
					},
					ensure_ascii=False,
					separators=(',', ':'),
				)
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				_record_exploration_decision(
					exploration_tracker,
					step=step,
					observation=observation,
					rendered_observation=rendered_observation,
					decision=decision,
					outcome=last_outcome,
				)
				memory = self._remember(decision.memory)
				# A detected loop is a planning stall, not a browser failure, so it
				# must not consume the consecutive-action-error budget.
				recent_signatures.clear()
				continue

			# Persist the initiated action before awaiting the browser.  A task-wide
			# watchdog may cancel a slow browser operation, but its action, thought,
			# and step still belong in the final timeout artifact.
			step_record['outcome'] = 'Action started; browser result was not recorded yet.'
			outcome.steps.append(step_record)
			action_failed = False
			try:
				if decision.action == 'find_chart_data_requests':
					budget = self._chart_action_budget(decision.action, cursor=decision.chart_cursor is not None)
					if budget <= 0:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'timeout',
								'error': (
									'insufficient task time remains for cursor inspection and finish'
									if decision.chart_cursor is not None
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
								cursor=decision.chart_cursor,
								task_dir=self.task_dir,
								task_identity=self.task.prompt_payload(),
							),
							budget,
						)
						last_outcome = execution.output
						_merge_usage(outcome.usage, execution.usage)
						payload = self._register_chart_artifact(last_outcome)
						if decision.chart_cursor is not None:
							action_failed = not isinstance(payload, dict) or payload.get('status') in {'stale_state', 'timeout'}
						else:
							# A completed scan can validly leave the agent without normalized
							# data.  Those outcomes trigger the visual/first-party-export
							# fallback in the next prompt; treating them as browser failures
							# would spend the action-error budget before that recovery runs.
							action_failed = not isinstance(payload, dict) or payload.get('status') not in {
								'ready',
								'saved_raw_only',
								'no_match',
							}
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
			_record_exploration_decision(
				exploration_tracker,
				step=step,
				observation=observation,
				rendered_observation=rendered_observation,
				decision=decision,
				outcome=step_record['outcome'],
			)
			memory = self._remember(decision.memory)
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
			self._salvage_into(outcome, memory)

		outcome.duration_seconds = round(time.monotonic() - started_at, 3)
		return outcome
