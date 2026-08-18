from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, SystemMessage, UserMessage
from browser_use.webretriever.artifacts import (
	EXPLORATION_PATHS_FILENAME,
	MODEL_CALL_TIMING_FILENAME,
	MODEL_PROMPT_LOG_FILENAME,
	MODEL_PROMPT_LOG_FORMAT,
	STRATEGY_REVIEW_PROMPT_LOG_FILENAME,
	STRATEGY_REVIEW_PROMPT_LOG_FORMAT,
	STRUCTURED_MODEL_PROMPT_LOG_FORMAT,
	atomic_write_json,
	empty_model_call_timing_payload,
	model_prompt_log_metadata,
	prompt_text_lines,
)
from browser_use.webretriever.exploration_paths import ExplorationPathError, ExplorationPathTracker, PathJsonActionResult
from browser_use.webretriever.model_retry import (
	MODEL_RETRY_MAX_ATTEMPTS,
	invoke_with_reconnect_retries,
)
from browser_use.webretriever.model_retry import (
	await_with_hard_timeout as _await_with_hard_timeout,
)
from browser_use.webretriever.models import ACTION_PARAMETER_CONTRACTS, AgentDecision, CompetitionTask, WebRetrieverActionResult
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
from browser_use.webretriever.strategy import StrategyReviewRequest
from browser_use.webretriever.verification import VerificationAction, VerificationController

_FIND_CHART_MAX_SECONDS = 60.0
_ANALYSIS_MAX_SECONDS = 90.0
_FINISH_RESERVE_SECONDS = 30.0
_FINISH_FALSE_RETRY_PREFIX = '你拥有强大的浏览器操作能力，你的任务是：'
_FINISH_FALSE_RETRY_SUFFIX = '这个任务是一定可以完成的，当前尚未完成，请继续完成任务。'


def _finish_false_retry_message(task: str) -> str:
	"""Build the authoritative continuation prompt after ``finish(false)``."""

	return f'{_FINISH_FALSE_RETRY_PREFIX}{task.strip()}。{_FINISH_FALSE_RETRY_SUFFIX}'


class _DecisionTaskDeadline(TimeoutError):
	"""The task deadline elapsed before a decision-model request could start."""


class _InvalidStructuredDecision(ValueError):
	"""A model response that cannot safely become one executable decision.

	The original provider/parser exception can contain arbitrary model text.  This
	exception deliberately carries only a bounded, schema-derived diagnostic that
	is safe to place in the next model prompt and durable artifacts.
	"""

	def __init__(self, diagnostic: str) -> None:
		super().__init__(diagnostic)
		self.diagnostic = diagnostic


_KNOWN_ACTION_NAMES = frozenset(
	{
		'click',
		'double_click',
		'type',
		'select',
		'press',
		'scroll',
		'hover',
		'click_xy',
		'hover_xy',
		'drag',
		'back',
		'navigate',
		'wait',
		'switch_tab',
		'close_tab',
		'read_element',
		'find_text',
		'inspect_network',
		'find_chart_data_requests',
		'call_data_analysis_assistant',
		'calculate',
		'finish',
	}
)
_KNOWN_DECISION_FIELDS = frozenset(AgentDecision.model_fields)
_SAFE_CONTRACT_DETAIL = re.compile(
	r'\b(?P<action>[a-z_]{1,64})\s+(?P<verb>requires|does not accept):\s*'
	r'(?P<fields>[a-z_]{1,64}(?:\s*,\s*[a-z_]{1,64})*)',
	re.IGNORECASE,
)


def _safe_contract_diagnostic(message: str) -> str:
	"""Turn an untrusted parser message into a small schema-only correction.

	Provider validation errors often embed the complete rejected JSON under
	``input_value``.  The model must learn *which contract rule failed*, but it
	must never receive that arbitrary payload verbatim on the next turn.
	"""

	for match in _SAFE_CONTRACT_DETAIL.finditer(message):
		action = match.group('action').casefold()
		fields = [field.strip() for field in match.group('fields').split(',') if field.strip()]
		if action not in _KNOWN_ACTION_NAMES or not fields or any(field not in _KNOWN_DECISION_FIELDS for field in fields):
			continue
		if match.group('verb').casefold() == 'requires':
			return f'action {action!r} requires field(s): {", ".join(fields)}'
		return f'action {action!r} does not accept field(s): {", ".join(fields)}'

	lowered = message.casefold()
	if 'field required' in lowered or 'missing' in lowered:
		# Pydantic's common multiline representation places the field name on
		# the preceding line.  Accept only known schema field names.
		lines = [line.strip() for line in message.splitlines()]
		for index, line in enumerate(lines):
			if 'field required' not in line.casefold():
				continue
			for previous in reversed(lines[:index]):
				candidate = previous.strip(' `.:')
				if candidate in _KNOWN_DECISION_FIELDS:
					return f'required decision field is missing: {candidate}'
		return 'a required decision field is missing'
	if 'extra_forbidden' in lowered or 'extra inputs are not permitted' in lowered:
		return 'decision contains a field outside the supported schema'
	if 'invalid json' in lowered or 'json decode' in lowered or 'failed to parse structured output' in lowered:
		return 'decision is not valid structured JSON for AgentDecision'
	if 'path_json_action' in lowered:
		return 'path_json_action has an invalid structured shape'
	if 'action' in lowered:
		return 'action must be a supported browser action with its required fields'
	return 'decision does not match the AgentDecision schema'


def _validation_error_diagnostic(error: ValidationError) -> str:
	"""Render Pydantic validation failure without inspecting rejected values."""

	details = error.errors(include_url=False, include_context=False, include_input=False)
	# Prefer a selected-action contract correction over a simultaneously emitted
	# generic extra-field error.  It tells the model exactly why the proposed
	# browser action could not run.
	for detail in details:
		message = str(detail.get('msg', ''))
		contract = _safe_contract_diagnostic(message)
		if contract.startswith('action '):
			return contract

	for detail in details:
		location = detail.get('loc', ())
		field_name = location[-1] if isinstance(location, tuple) and location else None
		if isinstance(field_name, str) and field_name in _KNOWN_DECISION_FIELDS:
			if detail.get('type') == 'missing':
				return f'required decision field is missing: {field_name}'
			if field_name == 'action':
				return 'action must be a supported browser action'
			return f'decision field {field_name!r} has an invalid type, value, or range'
		if detail.get('type') == 'extra_forbidden':
			return 'decision contains a field outside the supported schema'
	for detail in details:
		contract = _safe_contract_diagnostic(str(detail.get('msg', '')))
		if contract != 'decision does not match the AgentDecision schema':
			return contract
	return 'decision does not match the AgentDecision schema'


def _structured_output_error_diagnostic(error: Exception) -> str | None:
	"""Classify only parser/schema failures; transport and auth errors stay fatal."""

	if isinstance(error, ValidationError):
		return _validation_error_diagnostic(error)

	# Model adapters commonly wrap Pydantic failures in ModelProviderError.
	# Do not classify arbitrary provider failures here: authentication, service,
	# and connection failures retain their existing retry/failure semantics.
	message = str(error)
	lowered = message.casefold()
	if isinstance(error, ModelProviderError) and (
		error.status_code not in {401, 403}
		and (
			'validation error for agentdecision' in lowered
			or 'failed to parse structured output' in lowered
			or 'structured output validation' in lowered
		)
	):
		return _safe_contract_diagnostic(message)
	return None


def _raw_completion_contract_diagnostic(completion: Any) -> str | None:
	"""Recover a missing selected-action field even alongside unrelated extras.

	Pydantic stops its model-level action validator when an unknown top-level
	field is present.  We still want the repair prompt to explain that a
	``click`` lacks ``element_id`` rather than merely mentioning the extra field.
	Only action and known field *names* are examined; arbitrary values stay
	unread and never enter diagnostics.
	"""

	if not isinstance(completion, Mapping):
		return None
	candidate = completion
	action = candidate.get('action')
	if isinstance(action, Mapping):
		nested = action
		action = nested.get('action')
		candidate = {**candidate, **nested}
	if not isinstance(action, str) or action not in _KNOWN_ACTION_NAMES:
		return None
	contract = ACTION_PARAMETER_CONTRACTS[action]
	missing = sorted(field_name for field_name in contract.required if candidate.get(field_name) is None)
	if not missing:
		return None
	return f'action {action!r} requires field(s): {", ".join(missing)}'


def _coerce_agent_decision(completion: Any) -> AgentDecision:
	"""Validate an adapter completion even when it bypassed its output parser."""

	if isinstance(completion, AgentDecision):
		return completion
	try:
		return AgentDecision.model_validate(completion)
	except ValidationError as error:
		diagnostic = _raw_completion_contract_diagnostic(completion) or _validation_error_diagnostic(error)
		raise _InvalidStructuredDecision(diagnostic) from error


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
	verification: dict[str, object] = field(default_factory=dict)
	model_call_timing_summary: dict[str, int | float] = field(
		default_factory=lambda: dict(empty_model_call_timing_payload()['summary'])
	)


def _decision_action_payload(decision: AgentDecision) -> dict[str, Any]:
	payload = decision.action_payload()
	for field_name in ('thought', 'memory', 'answer', 'evidence', 'success'):
		if decision.action != 'finish' or field_name in ('thought', 'memory'):
			payload.pop(field_name, None)
	return payload


def _append_path_action_feedback(action_outcome: str, feedback: str) -> str:
	"""Attach path-tree diagnostics without invalidating a structured action result."""

	if not feedback:
		return action_outcome
	try:
		payload = json.loads(action_outcome)
	except (TypeError, json.JSONDecodeError):
		payload = None
	if isinstance(payload, dict):
		payload['path_json_action_feedback'] = feedback
		return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
	return f'{action_outcome}\nPath JSON action feedback: {feedback}'


def _path_json_action_artifact_payload(decision: AgentDecision) -> dict[str, Any]:
	"""Persist only declared path-operation fields, never arbitrary model extras.

	``PathJsonOperation`` deliberately accepts extra fields so the executor can
	ignore them. The execution report retains their names where useful; durable
	step artifacts must not retain their untrusted values.
	"""

	return {
		'operations': [
			operation.model_dump(
				mode='json',
				exclude=set(operation.model_extra or {}),
				exclude_none=True,
			)
			for operation in decision.path_json_action.operations
		]
	}


def _action_string(decision: AgentDecision) -> str:
	return json.dumps(_decision_action_payload(decision), ensure_ascii=False, separators=(',', ':'))


def _format_runtime_action_result(result: WebRetrieverActionResult | str) -> tuple[str, dict[str, Any] | None, bool]:
	"""Adapt the runtime seam to the prompt string and durable step payloads."""

	if isinstance(result, WebRetrieverActionResult):
		payload = result.model_dump(mode='json', exclude_none=True)
		if result.state_changed is None:
			payload['state_changed'] = None
		return result.to_prompt_text(), payload, result.status == 'error'
	text = str(result)
	return text, None, text.startswith('ERROR:')


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


def _no_change_signature(decision: AgentDecision, observation_fingerprint: str) -> tuple[str, str, str]:
	"""Identify one target/action on one observed page generation."""

	payload = _decision_action_payload(decision)
	target_fields = {
		name: payload[name]
		for name in ('element_id', 'selector', 'target', 'x', 'y', 'end_x', 'end_y', 'url')
		if payload.get(name) is not None
	}
	target = json.dumps(
		target_fields,
		ensure_ascii=False,
		sort_keys=True,
		separators=(',', ':'),
	)
	return observation_fingerprint, _action_intent(decision), target


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


def _exploration_trajectory_record(
	*,
	step: int,
	observation: Any,
	rendered_observation: str,
	decision: AgentDecision,
	outcome: str,
) -> dict[str, Any]:
	"""Build one durable trajectory record including model-declared progress."""

	return {
		'step': step + 1,
		'url': str(getattr(observation, 'url', '')),
		'title': str(getattr(observation, 'title', '')),
		'page_observation': _compact_checkpoint_observation(rendered_observation),
		'action': _decision_action_payload(decision),
		'current_path_id': decision.current_path_id,
		'progress': decision.progress,
		'path_json_action': _path_json_action_artifact_payload(decision),
		'outcome': _compact_checkpoint_observation(outcome, limit=1_200),
	}


def _record_exploration_decision(
	tracker: ExplorationPathTracker,
	*,
	step: int,
	observation: Any,
	rendered_observation: str,
	decision: AgentDecision,
	outcome: str,
) -> None:
	"""Commit model progress after a completed action and retain its trajectory data."""
	if tracker.answer_priority_mode:
		return

	# The caller owns ``outcome.steps``; this helper retains the same compact
	# browser-grounded shape for code paths that need an independent record.
	_exploration_trajectory_record(
		step=step,
		observation=observation,
		rendered_observation=rendered_observation,
		decision=decision,
		outcome=outcome,
	)
	tracker.record_decision(current_path_id=decision.current_path_id, progress=decision.progress)


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
		self.task = task
		self.llm = llm
		self.runtime = runtime
		self.task_dir = Path(task_dir)
		self.max_steps = max_steps
		self.model_timeout_seconds = model_timeout_seconds
		self.max_consecutive_action_errors = max_consecutive_action_errors
		self.max_consecutive_model_output_errors = max_consecutive_model_output_errors
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
		self._model_call_timing_path = self.task_dir / MODEL_CALL_TIMING_FILENAME
		self._model_call_timing: dict[str, Any] = empty_model_call_timing_payload()
		self._strategy_review_prompt_log_path = self.task_dir / STRATEGY_REVIEW_PROMPT_LOG_FILENAME
		self._strategy_review_prompt_log: dict[str, Any] = {}
		self.task_deadline_monotonic = task_deadline_monotonic
		self._trusted_data_manifests: dict[str, str] = {}
		self._ready_data_dirs: set[str] = set()
		self._data_artifact_filters: dict[str, dict[str, Any]] = {}
		self._announced_data_artifact_ids: set[str] = set()
		self._announced_download_timeout_keys: set[str] = set()
		self.chart_network_inspector = chart_network_inspector or ChartNetworkInspector(
			llm,
			# Leave room inside the 300-second task watchdog for normalization,
			# the 90-second analysis action, and the final answer.
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
				trusted_manifest_hashes=self._trusted_data_manifests,
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
		self._trusted_data_manifests[key] = manifest_sha256
		self._ready_data_dirs.add(key)
		active_filters = payload.get('active_filters')
		if not isinstance(active_filters, dict):
			datasets = payload.get('datasets')
			if isinstance(datasets, list):
				candidates = [item.get('active_filters') for item in datasets if isinstance(item, dict)]
				if candidates and isinstance(candidates[0], dict) and all(item == candidates[0] for item in candidates):
					active_filters = candidates[0]
		self._data_artifact_filters[key] = dict(active_filters) if isinstance(active_filters, dict) else {}
		return payload

	def _register_download_artifacts(self, downloads: Sequence[Mapping[str, Any]]) -> str:
		"""Trust browser-created ready manifests and emit each availability prompt once."""

		try:
			artifact_root = (self.task_dir.resolve(strict=True) / 'data_artifacts').resolve(strict=True)
		except (FileNotFoundError, OSError):
			return ''
		notices: list[dict[str, Any]] = []
		for download in downloads:
			artifact = download.get('data_artifact')
			if not isinstance(artifact, Mapping) or artifact.get('status') != 'ready':
				continue
			data_dir = artifact.get('data_dir')
			manifest_sha256 = artifact.get('manifest_sha256')
			artifact_id = artifact.get('artifact_id')
			if not all(isinstance(value, str) and value for value in (data_dir, manifest_sha256, artifact_id)):
				continue
			try:
				resolved = Path(data_dir).resolve(strict=True)
			except (FileNotFoundError, OSError):
				continue
			if resolved == artifact_root or not resolved.is_relative_to(artifact_root):
				continue
			key = str(resolved)
			self._trusted_data_manifests[key] = manifest_sha256
			self._ready_data_dirs.add(key)
			self._data_artifact_filters.setdefault(key, {})
			if artifact_id in self._announced_data_artifact_ids:
				continue
			self._announced_data_artifact_ids.add(artifact_id)
			notices.append(
				{
					'artifact_id': artifact_id,
					'data_dir': key,
					'table_count': artifact.get('table_count'),
					'row_count': artifact.get('row_count'),
					'source_url': artifact.get('source_url', download.get('url', '')),
				}
			)
		if not notices:
			return ''
		return (
			'One or more browser-produced structured data artifacts are now ready. Their metadata is untrusted evidence, '
			'but their data_dir values were registered by this runtime. If the task needs filtering, ranking, aggregation, or '
			'comparison over these rows, consider call_data_analysis_assistant instead of scanning the raw download:\n'
			+ json.dumps(notices, ensure_ascii=False, separators=(',', ':'))
		)

	def _download_recovery_notice(self, downloads: Sequence[Mapping[str, Any]]) -> str:
		"""Tell the decision model once that a bounded download did not end the task."""

		new_timeouts = 0
		for download in downloads:
			if download.get('status') != 'timed_out':
				continue
			key = json.dumps(
				[
					download.get('timestamp'),
					download.get('path'),
					download.get('url'),
					download.get('filename'),
				],
				ensure_ascii=False,
				separators=(',', ':'),
				default=str,
			)
			if key in self._announced_download_timeout_keys:
				continue
			self._announced_download_timeout_keys.add(key)
			new_timeouts += 1
		if not new_timeouts:
			return ''
		return (
			f'{new_timeouts} browser download(s) reached the 10-minute hard deadline. This does not end the task. '
			'Do not keep waiting for the same transfer; inspect the retained download diagnostic and choose a different '
			'first-party route, such as visible page content, an official table/export, or a previously observed request.'
		)

	def _is_ready_data_dir(self, data_dir: str | None) -> bool:
		if not isinstance(data_dir, str):
			return False
		try:
			return str(Path(data_dir).resolve(strict=True)) in self._ready_data_dirs
		except (FileNotFoundError, OSError):
			return False

	def _data_filter_mismatch(self, data_dir: str | None, analysis_query: str | None) -> str | None:
		if not isinstance(data_dir, str):
			return None
		try:
			filters = self._data_artifact_filters.get(str(Path(data_dir).resolve(strict=True)), {})
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

	def _reset_model_call_timing(self) -> None:
		"""Start a fresh, durable per-step table for decision-model waiting."""

		self._model_call_timing = empty_model_call_timing_payload()
		atomic_write_json(self._model_call_timing_path, self._model_call_timing)

	@property
	def model_call_timing_payload(self) -> dict[str, Any]:
		"""Return the current timing table for the runner's final artifact write."""

		return self._model_call_timing

	def _update_timing_summary(self, outcome: AgentRunOutcome) -> None:
		summary = self._model_call_timing.get('summary', {})
		if not isinstance(summary, dict):
			raise TypeError('model call timing summary must be an object')
		outcome.model_call_timing_summary = dict(summary)

	def _timing_step(self, step: int) -> dict[str, Any]:
		steps = self._model_call_timing.get('steps')
		if not isinstance(steps, list):
			raise TypeError('model call timing steps must be a list')
		if steps and steps[-1].get('step') == step + 1:
			return steps[-1]
		entry = {
			'step': step + 1,
			'model_wait_seconds': 0.0,
			'retry_wait_seconds': 0.0,
			'total_wait_seconds': 0.0,
			'attempts': [],
		}
		steps.append(entry)
		summary = self._model_call_timing['summary']
		summary['decision_step_count'] = len(steps)
		return entry

	def _record_model_attempt(
		self,
		*,
		step: int,
		attempt: int,
		status: str,
		wait_seconds: float,
		error: str | None = None,
	) -> None:
		"""Persist one completed model wait before any reconnection delay."""

		entry = self._timing_step(step)
		attempts = entry['attempts']
		if not isinstance(attempts, list):
			raise TypeError('model call timing attempts must be a list')
		attempt_entry: dict[str, Any] = {
			'attempt': attempt,
			'status': status,
			'model_wait_seconds': round(max(0.0, wait_seconds), 3),
			'retry_wait_seconds': 0.0,
			'total_wait_seconds': round(max(0.0, wait_seconds), 3),
		}
		if error:
			attempt_entry['error'] = error[:1_000]
		attempts.append(attempt_entry)
		entry['model_wait_seconds'] = round(float(entry['model_wait_seconds']) + attempt_entry['model_wait_seconds'], 3)
		entry['total_wait_seconds'] = round(float(entry['total_wait_seconds']) + attempt_entry['total_wait_seconds'], 3)
		summary = self._model_call_timing['summary']
		summary['attempt_count'] += 1
		summary[f'{status}_attempt_count'] += 1
		summary['model_wait_seconds'] = round(float(summary['model_wait_seconds']) + attempt_entry['model_wait_seconds'], 3)
		summary['total_wait_seconds'] = round(float(summary['total_wait_seconds']) + attempt_entry['total_wait_seconds'], 3)
		atomic_write_json(self._model_call_timing_path, self._model_call_timing)

	def _record_retry_wait(self, *, step: int, attempt: int, wait_seconds: float) -> None:
		"""Persist the actual fixed reconnection delay after a retryable failure."""

		entry = self._timing_step(step)
		attempts = entry['attempts']
		if not isinstance(attempts, list) or not attempts or attempts[-1].get('attempt') != attempt:
			raise ValueError('retry wait must follow its matching model attempt')
		actual_wait = round(max(0.0, wait_seconds), 3)
		attempts[-1]['retry_wait_seconds'] = actual_wait
		attempts[-1]['total_wait_seconds'] = round(float(attempts[-1]['model_wait_seconds']) + actual_wait, 3)
		entry['retry_wait_seconds'] = round(float(entry['retry_wait_seconds']) + actual_wait, 3)
		entry['total_wait_seconds'] = round(float(entry['total_wait_seconds']) + actual_wait, 3)
		summary = self._model_call_timing['summary']
		summary['retry_wait_seconds'] = round(float(summary['retry_wait_seconds']) + actual_wait, 3)
		summary['total_wait_seconds'] = round(float(summary['total_wait_seconds']) + actual_wait, 3)
		atomic_write_json(self._model_call_timing_path, self._model_call_timing)

	async def _invoke_decision_with_retries(
		self,
		*,
		messages: list[Any],
		step: int,
		outcome: AgentRunOutcome,
	) -> Any:
		"""Call the decision model through at most five freshly-created clients.

		``ChatOpenAI.ainvoke`` creates its AsyncOpenAI client for each invocation,
		so retrying here deliberately abandons a failed connection.  The client
		itself is configured with zero retries to keep the five-attempt contract
		exact rather than nested.
		"""

		def record_attempt(attempt: int, status: str, wait_seconds: float, error: Exception | None) -> None:
			self._record_model_attempt(
				step=step,
				attempt=attempt,
				status=status,
				wait_seconds=wait_seconds,
				error=f'{type(error).__name__}: {error}' if error is not None else None,
			)
			self._update_timing_summary(outcome)

		def record_retry_wait(attempt: int, wait_seconds: float) -> None:
			self._record_retry_wait(step=step, attempt=attempt, wait_seconds=wait_seconds)
			self._update_timing_summary(outcome)

		return await invoke_with_reconnect_retries(
			lambda: self.llm.ainvoke(messages, output_format=AgentDecision),
			timeout_seconds=lambda: min(self.model_timeout_seconds, self._remaining_task_seconds()),
			on_attempt_finished=record_attempt,
			on_retry_wait_finished=record_retry_wait,
		)

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
	) -> int:
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
		prompt_index = len(steps)
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
		return prompt_index

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
		prompt_index: int | None = None,
		started_at: float,
		usage: Mapping[str, int] | None = None,
		error: str | None = None,
	) -> None:
		"""Add call results after the already-durable structured model input."""

		if not self.structured_prompt_log:
			return
		steps = self._model_prompt_log.get('steps')
		resolved_prompt_index = step if prompt_index is None else prompt_index
		if not isinstance(steps, list) or resolved_prompt_index >= len(steps):
			raise ValueError('model prompt result has no matching persisted input')
		entry = steps[resolved_prompt_index]
		if not isinstance(entry, dict):
			raise TypeError('structured model prompt step must be an object')
		entry['model_call'] = {
			'duration_seconds': round(max(0.0, time.monotonic() - started_at), 3),
			'usage': dict(usage or {}),
			'error': error,
		}
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)

	async def _request_model_decision(
		self,
		*,
		step: int,
		context: StepContext,
		screenshot: bytes,
		raw_path: Path,
		outcome: AgentRunOutcome,
	) -> tuple[AgentDecision, dict[str, int], int, float]:
		"""Request one decision while allowing path-tree retries to reuse a step.

		The caller may invoke this more than once for the same ``step`` when the
		model's path-tree delta is not executable.  Prompt-log entries are therefore
		indexed by their append position rather than by the competition step number.
		"""

		prompt_document = self.prompt_composer.compose_step(context)
		messages = [
			SystemMessage(content=self.system_prompt),
			UserMessage(
				content=[
					ContentPartTextParam(text=prompt_document.text),
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
			raise _DecisionTaskDeadline()
		prompt_index = self._record_model_prompt(
			step=step,
			prompt_document=prompt_document,
			screenshot_path=raw_path,
		)
		model_call_started_at = time.monotonic()
		try:
			response = await self._invoke_decision_with_retries(messages=messages, step=step, outcome=outcome)
		except TimeoutError:
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				error=(
					f'Model request exceeded {model_call_timeout:g} seconds after up to '
					f'{MODEL_RETRY_MAX_ATTEMPTS} connection attempts'
				),
			)
			raise
		except Exception as exc:
			diagnostic = _structured_output_error_diagnostic(exc)
			if diagnostic is not None:
				model_error = f'Model structured decision is invalid: {diagnostic}'
				self._record_model_result(
					step=step,
					prompt_index=prompt_index,
					started_at=model_call_started_at,
					error=model_error,
				)
				raise _InvalidStructuredDecision(diagnostic) from exc
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				error=f'{type(exc).__name__}: {exc}',
			)
			raise

		model_usage = _usage_dict(getattr(response, 'usage', None))
		_merge_usage(outcome.usage, model_usage)
		try:
			decision = _coerce_agent_decision(getattr(response, 'completion', None))
		except _InvalidStructuredDecision as exc:
			model_error = f'Model structured decision is invalid: {exc.diagnostic}'
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				usage=model_usage,
				error=model_error,
			)
			raise
		self._record_model_result(
			step=step,
			prompt_index=prompt_index,
			started_at=model_call_started_at,
			usage=model_usage,
		)
		return decision, model_usage, prompt_index, model_call_started_at

	async def run(self) -> AgentRunOutcome:
		started_at = time.monotonic()
		outcome = AgentRunOutcome(status='FAIL')
		self._partial_outcome = outcome
		self._reset_model_prompt_log()
		self._reset_model_call_timing()
		memory = ''
		last_outcome = 'The task has just started.'
		consecutive_errors = 0
		consecutive_model_output_errors = 0
		last_action_signature: tuple[str, str] | None = None
		repeated_action_count = 0
		# Rolling (intent, observation) window that survives parameter churn and
		# action alternation, which the exact-repeat guard below cannot see.
		recent_signatures: deque[tuple[str, str]] = deque(maxlen=16)
		no_change_counts: dict[tuple[str, str, str], int] = {}
		blocked_no_change_signatures: set[tuple[str, str, str]] = set()
		blocked_loop_intents: set[str] = set()
		exploration_paths_path = self.task_dir / EXPLORATION_PATHS_FILENAME
		exploration_tracker = ExplorationPathTracker(
			task_id=self.task.task_id,
			on_change=lambda payload: atomic_write_json(exploration_paths_path, payload),
		)
		atomic_write_json(exploration_paths_path, exploration_tracker.payload())
		verification = VerificationController(target_url=self.task.website)

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

			verification_decision = verification.decide(observation)
			if verification_decision.action is VerificationAction.BLOCKED:
				step_record = {
					'step': step,
					'url': observation.url,
					'thought': '验证状态机：可见验证在有界等待后仍未完成。',
					'action': {'action': 'verification_blocked'},
					'path_json_action': {'operations': []},
					'outcome': verification_decision.reason,
				}
				outcome.thoughts.append(step_record['thought'])
				outcome.steps.append(step_record)
				outcome.status = 'FAIL_VERIFICATION'
				outcome.error = verification_decision.reason
				break
			if verification_decision.action in {VerificationAction.CLICK, VerificationAction.WAIT}:
				payload: dict[str, Any]
				if verification_decision.action is VerificationAction.CLICK:
					payload = {'action': 'click', 'element_id': verification_decision.element_id}
				else:
					payload = {'action': 'wait', 'seconds': verification_decision.wait_seconds}
				action_text = json.dumps(
					{**payload, 'source': 'verification_controller'}, ensure_ascii=False, separators=(',', ':')
				)
				thought = f'验证状态机：{verification_decision.reason}。'
				_save_visual_screenshot(
					screenshot,
					self.task_dir / 'trajectory_visual' / f'{step}.png',
					action_text,
					observation,
					verification_decision.element_id,
				)
				outcome.actions.append(action_text)
				outcome.thoughts.append(thought)
				step_record = {
					'step': step,
					'url': observation.url,
					'thought': thought,
					'action': payload,
					'path_json_action': {'operations': []},
				}
				try:
					runtime_result = await self.runtime.execute(payload)
					last_outcome, action_result_payload, action_failed = _format_runtime_action_result(runtime_result)
				except Exception as exc:
					last_outcome = f'ERROR: {type(exc).__name__}: {exc}'
					action_result_payload = None
					action_failed = True
				if action_result_payload is not None:
					step_record['action_result'] = action_result_payload
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				if action_failed:
					consecutive_errors += 1
				else:
					consecutive_errors = 0
				if consecutive_errors >= self.max_consecutive_action_errors:
					outcome.status = 'FAIL_ACTIONS'
					outcome.error = f'{consecutive_errors} consecutive browser actions failed; last error: {last_outcome}'
					break
				continue
			if verification_decision.state.value == 'passed' and not last_outcome.startswith(_FINISH_FALSE_RETRY_PREFIX):
				last_outcome = 'Visible verification completed; continue in the same browser context.'

			downloads = list(getattr(observation, 'downloads', []) or [])
			data_artifact_notice = self._register_download_artifacts(downloads)
			download_recovery_notice = self._download_recovery_notice(downloads)
			rendered_observation = observation.render_text()
			observation_fingerprint = _observation_hash(rendered_observation)
			remaining = self._remaining_task_seconds()
			remaining_task_seconds = None if remaining == float('inf') else remaining
			answer_priority_mode = exploration_tracker.answer_priority_mode
			exploration_review = exploration_tracker.review_request(current_page_url=observation.url)
			path_tree_ready = False
			while not path_tree_ready:
				try:
					decision, model_usage, prompt_index, model_call_started_at = await self._request_model_decision(
						step=step,
						context=StepContext(
							step_index=step,
							observation=observation,
							history=tuple(outcome.steps),
							memory=memory,
							last_outcome=last_outcome,
							remaining_task_seconds=remaining_task_seconds,
							exploration_paths=None if answer_priority_mode else exploration_tracker.payload(),
							exploration_review=exploration_review,
							unseen_page_exploration=(
								False if answer_priority_mode else exploration_tracker.current_observation_is_unseen_page
							),
							path_consecutive_no_progress=(
								0 if answer_priority_mode else exploration_tracker.consecutive_no_progress
							),
							answer_priority_mode=answer_priority_mode,
							data_artifact_notice=data_artifact_notice,
							download_recovery_notice=download_recovery_notice,
						),
						screenshot=screenshot,
						raw_path=raw_path,
						outcome=outcome,
					)
				except PromptError as exc:
					outcome.status = 'FAIL_PROMPT'
					outcome.error = f'Prompt composition failed: {type(exc).__name__}: {exc}'
					break
				except _InvalidStructuredDecision as exc:
					model_error = f'Model structured decision is invalid: {exc.diagnostic}'
					_save_visual_screenshot(
						screenshot,
						self.task_dir / 'trajectory_visual' / f'{step}.png',
						model_error,
						observation,
					)
					consecutive_model_output_errors += 1
					last_outcome = (
						'ERROR: The previous structured decision was invalid; no browser action was executed. '
						f'Diagnostic: {exc.diagnostic}. Correct the action and return one valid AgentDecision; '
						'this retry does not consume a step.'
					)
					if consecutive_model_output_errors >= self.max_consecutive_model_output_errors:
						outcome.status = 'FAIL_MODEL'
						outcome.error = (
							f'Model returned {consecutive_model_output_errors} consecutive invalid structured decisions; '
							f'last diagnostic: {exc.diagnostic}'
						)
						break
					continue
				except _DecisionTaskDeadline:
					outcome.status = 'FAIL_TASK_TIMEOUT'
					outcome.error = 'Task deadline elapsed before the next model decision'
					self._salvage_into(outcome, memory)
					break
				except TimeoutError:
					model_call_timeout = min(self.model_timeout_seconds, self._remaining_task_seconds())
					model_error = (
						f'Model request exceeded {model_call_timeout:g} seconds after up to '
						f'{MODEL_RETRY_MAX_ATTEMPTS} connection attempts'
					)
					_save_visual_screenshot(
						screenshot,
						self.task_dir / 'trajectory_visual' / f'{step}.png',
						model_error,
						observation,
					)
					last_outcome = (
						f'ERROR: The decision model timed out after up to {MODEL_RETRY_MAX_ATTEMPTS} connection attempts; '
						'no browser action was executed.'
					)
					outcome.steps.append(
						{
							'step': step,
							'url': observation.url,
							'thought': '',
							'action': {},
							'path_json_action': {'operations': []},
							'outcome': last_outcome,
						}
					)
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
					last_outcome = 'ERROR: The decision model request failed; no browser action was executed.'
					outcome.steps.append(
						{
							'step': step,
							'url': observation.url,
							'thought': '',
							'action': {},
							'path_json_action': {'operations': []},
							'outcome': last_outcome,
						}
					)
					outcome.status = 'FAIL_MODEL'
					outcome.error = model_error
					break
				path_error: str | None = None
				answer = (decision.answer or '').strip()
				evidence = [item.strip() for item in (decision.evidence or []) if item.strip()]
				is_successful_finish = decision.action == 'finish' and decision.success is True and bool(answer and evidence)
				if is_successful_finish:
					# Answer delivery is independent of route bookkeeping.  In particular,
					# a first-page answer must not be rejected merely because the model did
					# not create or update path.json first.
					path_action_result = PathJsonActionResult(())
				elif answer_priority_mode:
					# Once a route succeeded, later decisions may do anything in the
					# browser without emitting or validating path metadata.
					path_action_result = PathJsonActionResult((), answer_priority_mode=True)
				else:
					try:
						path_action_result = exploration_tracker.apply_path_json_action_and_activate(
							decision.path_json_action,
							start_url=observation.url,
							current_path_id=decision.current_path_id,
							progress=decision.progress,
						)
						if path_action_result.answer_priority_mode:
							# The successful mutation itself is the mode switch.  Ignore any
							# remaining same-batch path operations and execute this decision.
							pass
						elif path_action_result.blocked or path_action_result.has_unapplied_operations:
							path_error = path_action_result.feedback() or 'one or more path JSON operations were not applied'
						else:
							exploration_tracker.accept_review(exploration_review)
					except ExplorationPathError as exc:
						path_action_result = PathJsonActionResult((), str(exc))
						path_error = str(exc)

				if path_error is not None:
					model_error = f'Model response omitted or invalidated exploration-path state: {path_error}'
					self._record_model_result(
						step=step,
						prompt_index=prompt_index,
						started_at=model_call_started_at,
						error=model_error,
					)
					_save_visual_screenshot(
						screenshot,
						self.task_dir / 'trajectory_visual' / f'{step}.png',
						model_error,
						observation,
					)
					consecutive_model_output_errors += 1
					last_outcome = (
						'ERROR: Exploration path JSON operation failed; no browser action was executed. '
						f'Diagnostic: {path_error[:1_000]} Correct path_json_action and retry; this retry does not consume a step.'
					)
					if consecutive_model_output_errors >= self.max_consecutive_model_output_errors:
						outcome.status = 'FAIL_MODEL'
						outcome.error = model_error
						break
					continue

				consecutive_model_output_errors = 0
				path_tree_ready = True

			if not path_tree_ready:
				break
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
				'current_path_id': decision.current_path_id,
				'progress': decision.progress,
				'path_json_action': _path_json_action_artifact_payload(decision),
				'path_json_action_result': path_action_result.payload(),
			}

			if decision.action == 'finish':
				if decision.success is True and answer and evidence:
					step_record['outcome'] = 'Task completed with an answer and origin explanation.'
					outcome.steps.append(step_record)
					outcome.status = 'SUCCESS'
					outcome.agent_answer = answer
					outcome.evidence = evidence
					break
				if decision.success is False:
					failure_reason = answer or 'Agent declared the task unsuccessful.'
					# Keep the model's declaration in the durable trajectory, but treat
					# ``finish(false)`` as recoverable.  The next decision receives an
					# explicit authoritative continuation instruction instead of a
					# terminal failure status.
					step_record['outcome'] = failure_reason
					outcome.steps.append(step_record)
					_record_exploration_decision(
						exploration_tracker,
						step=step,
						observation=observation,
						rendered_observation=rendered_observation,
						decision=decision,
						outcome=step_record['outcome'],
					)
					if decision.memory.strip():
						memory = self._remember(decision.memory)
					last_outcome = _finish_false_retry_message(self.task.task)
					continue
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

			action_intent = _action_intent(decision)
			no_change_signature = _no_change_signature(decision, observation_fingerprint)
			no_change_blocked = no_change_signature in blocked_no_change_signatures
			if not no_change_blocked:
				action_signature = (observation_fingerprint, action_text)
				if action_signature == last_action_signature:
					repeated_action_count += 1
				else:
					last_action_signature = action_signature
					repeated_action_count = 1
			if not no_change_blocked and repeated_action_count >= 3:
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

			recent_signatures.append((action_intent, observation_fingerprint))
			loop_report = (
				{
					'pattern': 'repeated_no_change',
					'attempts': no_change_counts.get(no_change_signature, 0),
					'error': 'the same page generation, action intent, and target produced no observable change twice',
				}
				if no_change_blocked
				else _detect_loop(recent_signatures)
			)
			if loop_report is not None:
				blocked_loop_intents.add(action_intent)
				last_outcome = json.dumps(
					{
						'action': decision.action,
						'status': 'repeated_no_change' if loop_report.get('pattern') == 'repeated_no_change' else 'loop_detected',
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
			action_result_payload: dict[str, Any] | None = None
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
						chart_payload = self._register_chart_artifact(last_outcome)
						if decision.chart_cursor is not None:
							action_failed = not isinstance(chart_payload, dict) or chart_payload.get('status') in {
								'stale_state',
								'timeout',
							}
						else:
							# A completed scan can validly leave the agent without normalized
							# data.  Those outcomes trigger the visual/first-party-export
							# fallback in the next prompt; treating them as browser failures
							# would spend the action-error budget before that recovery runs.
							action_failed = not isinstance(chart_payload, dict) or chart_payload.get('status') not in {
								'ready',
								'saved_raw_only',
								'no_match',
							}
				elif decision.action == 'call_data_analysis_assistant':
					budget = self._chart_action_budget(decision.action)
					filter_mismatch = self._data_filter_mismatch(decision.data_dir, decision.analysis_query)
					if not self._is_ready_data_dir(decision.data_dir):
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'invalid_data_dir',
								'error': 'data_dir must be the exact ready directory returned by a data artifact in this task run',
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
							analysis_payload: Any = json.loads(last_outcome)
						except (TypeError, json.JSONDecodeError):
							analysis_payload = None
						action_failed = not isinstance(analysis_payload, dict) or analysis_payload.get('status') != 'ok'
				else:
					runtime_result = await self.runtime.execute(decision)
					last_outcome, action_result_payload, action_failed = _format_runtime_action_result(runtime_result)
			except TimeoutError:
				last_outcome = json.dumps(
					{'action': decision.action, 'status': 'timeout', 'error': 'action exceeded its shared task budget'},
					separators=(',', ':'),
				)
				action_failed = True
			except Exception as exc:
				last_outcome = f'ERROR: {type(exc).__name__}: {exc}'
				action_failed = True

			path_action_feedback = path_action_result.feedback()
			if path_action_feedback:
				last_outcome = _append_path_action_feedback(last_outcome, path_action_feedback)

			if action_result_payload is not None:
				step_record['action_result'] = action_result_payload
				if action_result_payload.get('status') == 'no_change':
					no_change_counts[no_change_signature] = no_change_counts.get(no_change_signature, 0) + 1
					if no_change_counts[no_change_signature] >= 2:
						blocked_no_change_signatures.add(no_change_signature)
				else:
					no_change_counts.pop(no_change_signature, None)

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
			outcome.error = f'Reached the competition limit of {self.max_steps} steps without a final answer'
			self._salvage_into(outcome, memory)

		outcome.duration_seconds = round(time.monotonic() - started_at, 3)
		outcome.verification = verification.summary()
		self._update_timing_summary(outcome)
		return outcome
