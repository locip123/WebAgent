from __future__ import annotations

import ast
import base64
import hashlib
import inspect
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ValidationError

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError, ModelStructuredOutputError
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, SystemMessage, UserMessage
from browser_use.llm.schema import SchemaOptimizer
from browser_use.webretriever.artifacts import (
	EXPLORATION_PATHS_FILENAME,
	MODEL_CALL_TIMING_FILENAME,
	MODEL_PROMPT_LOG_FILENAME,
	MODEL_PROMPT_LOG_FORMAT,
	STRUCTURED_MODEL_PROMPT_LOG_FORMAT,
	atomic_write_json,
	empty_model_call_timing_payload,
	model_prompt_log_metadata,
	prompt_text_lines,
)
from browser_use.webretriever.browser import is_browser_session_closed_error, redact_cdp_url
from browser_use.webretriever.browser_failures import BrowserFailurePhase, classify_browser_failure
from browser_use.webretriever.completion import (
	AnswerCandidate,
	CompletionGate,
	CompletionVerificationError,
	LLMCompletionReviewer,
	RequirementLedgerError,
)
from browser_use.webretriever.exploration_paths import (
	ExplorationPathError,
	ExplorationPathTracker,
	PathJsonActionResult,
)
from browser_use.webretriever.model_retry import (
	MODEL_RETRY_MAX_ATTEMPTS,
	invoke_with_reconnect_retries,
)
from browser_use.webretriever.model_retry import (
	await_with_hard_timeout as _await_with_hard_timeout,
)
from browser_use.webretriever.model_services import ModelServiceRouter, invoke_with_service_failover
from browser_use.webretriever.models import (
	ACTION_PARAMETER_CONTRACTS,
	AgentDecision,
	AgentDecisionEnvelope,
	CompetitionTask,
	InitialPageAgentDecisionEnvelope,
	RootUpdateRepairAgentDecisionEnvelope,
	WebRetrieverActionResult,
)
from browser_use.webretriever.network import ChartNetworkInspector
from browser_use.webretriever.prompts import (
	DEFAULT_THOUGHT_LANGUAGE,
	PromptComposer,
	PromptDocument,
	PromptError,
	PromptTarget,
	StepContext,
	StructuredDecisionRepairFeedback,
	normalize_thought_language,
)
from browser_use.webretriever.run_control import (
	CancellationToken,
	NoOpRunObserver,
	RunObserver,
	RunnerEvent,
	emit_safely,
)
from browser_use.webretriever.verification import VerificationAction, VerificationController

_FIND_CHART_MAX_SECONDS = 60.0
_ANALYSIS_MAX_SECONDS = 90.0
_FINISH_RESERVE_SECONDS = 30.0
_FINISH_FALSE_RETRY_PREFIX = 'You have powerful browser interaction capabilities. Your task is:'
_FINISH_FALSE_RETRY_SUFFIX = (
	'This task can be completed, but it is not complete yet. If the current approach does not work, '
	'find another solution or route and continue working on the task.'
)
_NON_RETRYABLE_ANALYSIS_STATUSES = frozenset({'analysis_unavailable', 'invalid_manifest', 'no_tabular_data'})
_INVALID_DECISION_SNAPSHOT_MAX_CHARACTERS = 8_000
_INVALID_DECISION_SNAPSHOT_TRUNCATION_MARKER = '\n...[previous_invalid_decision truncated]...\n'
_ACTION_CONTRACT_ERRORS_BEFORE_HIDE = 2
_RUNTIME_NOT_STARTED_ERROR = 'call browserruntime.start(website) first'
_RUNTIME_NOT_STARTED_RECOVERY_NOTICE = (
	'Browser runtime was restarted after its initial observation found it unstarted. '
	'The page below is a fresh observation; reassess it before acting.'
)
_TASK_PAGE_RECOVERY_NOTICE = (
	'Browser task page was rebuilt after it became unavailable. Any previous action was not replayed. '
	'The page below is a fresh observation in the same task context; reassess it before acting.'
)
_SURVIVING_TASK_PAGE_RECOVERY_NOTICE = (
	'Browser task page was unavailable, but a surviving task-owned page was re-grounded. '
	'Any previous action was not replayed; reassess this fresh observation before acting.'
)
_TASK_PAGE_RESTART_MAX_SECONDS = 60.0
_CLEAN_WORKER_RECOVERY_MAX_SECONDS = 160.0
_ANALYSIS_NOT_READY_RECOVERY = (
	'The data analysis assistant is currently unavailable because this task has no usable ready_data_dir.\n'
	'Documents under downloads/ are not analyzable data artifacts. Do not call call_data_analysis_assistant '
	'again until an observation contains a new, complete ready_data_dir.\n'
	'Use document evidence actions such as find_text or read_element instead, or continue searching '
	'official first-party sources for the evidence required by the task.'
)
_STALE_CLICK_RECOVERY_LAST_OUTCOME = (
	'A previous semantic click could not complete because its current element reference expired. '
	'A fresh browser observation and screenshot are required before one bounded recovery action.'
)


def _supports_completion_protocol(llm: Any) -> bool:
	"""Avoid consuming legacy decision-only model seams during capability setup."""

	if isinstance(llm, ModelServiceRouter):
		return all(_supports_completion_protocol(state.client) for state in llm._states)
	try:
		parameters = inspect.signature(llm.ainvoke).parameters
	except (AttributeError, TypeError, ValueError):
		return False
	return 'output_format' in parameters


def _recovery_error_text(error: BaseException) -> str:
	"""Bound and redact recovery diagnostics before persisting them."""

	return redact_cdp_url(f'{type(error).__name__}: {error}')[:500]


def _action_contracts_for_data_capability(
	eligible_data_dirs: tuple[str, ...],
	*,
	hidden_actions: Collection[str] = (),
) -> dict[str, Any]:
	"""Return one request-local action set without mutating the global contract.

	``hidden_actions`` only applies to same-step structured-output repair.  The
	``finish`` and ``submit_answer_candidate`` always remain available so the
	model can report a terminal failure or submit its candidate instead of being
	left with an empty action space.
	"""

	contracts = dict(ACTION_PARAMETER_CONTRACTS)
	if not eligible_data_dirs:
		contracts.pop('call_data_analysis_assistant')
	for action in hidden_actions:
		if action not in {'finish', 'submit_answer_candidate'}:
			contracts.pop(action, None)
	return contracts


def _is_stale_click_recovery_candidate(
	*,
	action: str,
	action_result_payload: Mapping[str, Any] | None,
) -> bool:
	"""Whether the action-error threshold may use the bounded click fallback."""

	return bool(
		action == 'click'
		and isinstance(action_result_payload, Mapping)
		and action_result_payload.get('status') == 'error'
		and action_result_payload.get('error_type') == 'StaleElement'
	)


def _stale_click_recovery_history(history: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
	"""Keep the stale failure durable without forwarding its raw details to recovery."""

	if not history:
		return ()
	redacted_history = list(history)
	latest = dict(redacted_history[-1])
	latest['action'] = {'action': 'click'}
	latest.pop('action_result', None)
	latest['outcome'] = _STALE_CLICK_RECOVERY_LAST_OUTCOME
	redacted_history[-1] = latest
	return tuple(redacted_history)


@lru_cache(maxsize=256)
def _capability_gated_output_format(
	*,
	initial_page: bool,
	root_update_repair: bool,
	eligible_data_dirs: tuple[str, ...],
	hidden_actions: tuple[str, ...],
) -> type[BaseModel]:
	"""Build an immutable provider schema variant for one task-local capability state."""

	base = (
		InitialPageAgentDecisionEnvelope
		if initial_page
		else RootUpdateRepairAgentDecisionEnvelope
		if root_update_repair
		else AgentDecisionEnvelope
	)
	contracts = _action_contracts_for_data_capability(eligible_data_dirs, hidden_actions=hidden_actions)
	attributes: dict[str, Any] = {'__structured_action_parameter_contracts__': contracts}
	if eligible_data_dirs:
		attributes['__structured_action_field_enums__'] = {
			'call_data_analysis_assistant': {'data_dir': eligible_data_dirs}
		}
	name = (
		'InitialPage'
		if initial_page
		else 'RootUpdateRepair'
		if root_update_repair
		else 'Standard'
	) + 'CapabilityGatedAgentDecisionEnvelope'
	return type(name, (base,), attributes)


def _model_output_protocol(
	llm: BaseChatModel,
	output_format: type[BaseModel] = AgentDecisionEnvelope,
) -> dict[str, Any]:
	"""Describe the exact structured-output contract sent with decision calls.

	The decision schema is transported separately from the text messages by most
	providers.  Keep both the source Pydantic schema and the provider-compatible
	strict schema in the artifact so a prompt log can be audited without having to
	reconstruct the adapter call later.
	"""

	remove_min_items = bool(getattr(llm, 'remove_min_items_from_schema', False))
	remove_defaults = bool(getattr(llm, 'remove_defaults_from_schema', False))
	provider_schema = SchemaOptimizer.create_optimized_json_schema(
		output_format,
		remove_min_items=remove_min_items,
		remove_defaults=remove_defaults,
	)
	json_schema = {
		'type': 'json_schema',
		'name': 'agent_output',
		'strict': True,
		'schema': provider_schema,
	}
	use_responses_api = bool(getattr(llm, 'use_responses_api', False))
	force_structured_output = not bool(getattr(llm, 'dont_force_structured_output', False))
	if use_responses_api:
		provider_request = {'text': {'format': json_schema}} if force_structured_output else {}
		transport = 'text.format'
	else:
		provider_request = (
			{
				'response_format': {
					'type': 'json_schema',
					'json_schema': {
						'name': 'agent_output',
						'strict': True,
						'schema': provider_schema,
					},
				}
			}
			if force_structured_output
			else {}
		)
		transport = 'response_format'

	def safe_provider() -> str | None:
		try:
			value = getattr(llm, 'provider', None)
		except Exception:
			return None
		return str(value) if value is not None else None

	decision_model = getattr(output_format, '__structured_decision_model__', AgentDecision)
	if not isinstance(decision_model, type) or not issubclass(decision_model, BaseModel):
		raise TypeError('structured decision output format must declare a Pydantic decision model')
	return {
		'source_model': f'{decision_model.__module__}.{decision_model.__qualname__}',
		'source_json_schema': decision_model.model_json_schema(),
		'provider_output_model': f'{output_format.__module__}.{output_format.__qualname__}',
		'provider_schema': json_schema,
		'provider_request': provider_request,
		'transport': transport,
		'structured_output_forced': force_structured_output,
		'schema_embedded_in_system_prompt': bool(getattr(llm, 'add_schema_to_system_prompt', False)),
		'provider': safe_provider(),
		'model': str(getattr(llm, 'model', '')),
		'adapter': f'{type(llm).__module__}.{type(llm).__qualname__}',
	}


def _finish_false_retry_message(task: str) -> str:
	"""Build the authoritative continuation prompt after ``finish(false)``."""

	return f'{_FINISH_FALSE_RETRY_PREFIX}\n{task.strip()}\n{_FINISH_FALSE_RETRY_SUFFIX}'


class _DecisionTaskDeadline(TimeoutError):
	"""The task deadline elapsed before a decision-model request could start."""


class _InvalidStructuredDecision(ValueError):
	"""A model response that cannot safely become one executable decision.

	The original provider/parser exception can contain arbitrary model text.  This
	exception carries a bounded, schema-derived diagnostic plus, when the adapter
	returned it, a bounded snapshot of the rejected completion for the next repair
	prompt.  The snapshot is explicitly untrusted model content.
	"""

	def __init__(
		self,
		diagnostic: str,
		previous_invalid_decision: str | None = None,
		source_service_group: str | None = None,
	) -> None:
		super().__init__(diagnostic)
		self.diagnostic = diagnostic
		self.previous_invalid_decision = previous_invalid_decision
		self.source_service_group = source_service_group


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
_ACTION_CONTRACT_DIAGNOSTIC = re.compile(
	r"^action '(?P<action>[a-z_]{1,64})' (?:requires field\(s\)|does not accept field\(s\)):",
	re.IGNORECASE,
)


def _safe_contract_diagnostic(message: str) -> str:
	"""Turn an untrusted parser message into a small schema-only correction.

	Provider validation errors often embed the complete rejected JSON under
	``input_value``.  The diagnostic remains schema-only; the separately extracted
	input snapshot is bounded and marked as untrusted before it reaches the model.
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
	if (
		'invalid json' in lowered
		or 'json decode' in lowered
		or 'extra data' in lowered
		or 'failed to parse structured output' in lowered
	):
		return 'decision is not valid structured JSON for AgentDecision'
	if 'path_json_action' in lowered:
		return 'path_json_action has an invalid structured shape'
	if 'action' in lowered:
		return 'action must be a supported browser action with its required fields'
	return 'decision does not match the AgentDecision schema'


def _action_name_from_contract_diagnostic(diagnostic: str) -> str | None:
	"""Return the known non-terminal action named by a schema-only diagnostic."""

	match = _ACTION_CONTRACT_DIAGNOSTIC.match(diagnostic)
	if match is None:
		return None
	action = match.group('action').casefold()
	return action if action in ACTION_PARAMETER_CONTRACTS and action != 'finish' else None


def _temporarily_hidden_action_diagnostic(diagnostic: str, action: str) -> str:
	"""Explain that a repeatedly malformed action was removed from this repair."""

	return (
		f'{diagnostic}. Action {action!r} has been temporarily removed from the remaining repair requests '
		'for this step. Choose another action that remains in the action contract.'
	)


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

	# Adapters sometimes add a generic provider wrapper around the parser error.
	# Walk only the short local exception chain; never treat arbitrary provider
	# text as a repair signal.
	to_visit: list[BaseException] = [error]
	seen: set[int] = set()
	while to_visit:
		current = to_visit.pop(0)
		if id(current) in seen:
			continue
		seen.add(id(current))
		if isinstance(current, ValidationError):
			return _validation_error_diagnostic(current)
		if isinstance(current, ModelStructuredOutputError):
			return _safe_contract_diagnostic(str(current))
		if isinstance(current, json.JSONDecodeError):
			return 'decision is not valid structured JSON for AgentDecision'

		# Do not classify arbitrary provider failures here: authentication,
		# service, and connection failures retain their existing semantics.
		message = str(current)
		lowered = message.casefold()
		if isinstance(current, ModelProviderError) and current.status_code not in {401, 403} and (
			'validation error for ' in lowered
			or 'failed to parse structured output' in lowered
			or 'structured output validation' in lowered
		):
			return _safe_contract_diagnostic(message)

		if not (isinstance(current, ModelProviderError) and current.status_code in {401, 403}):
			for related in (current.__cause__, current.__context__):
				if related is not None and id(related) not in seen:
					to_visit.append(related)
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


def _safe_repr(value: Any, *, max_characters: int = 2_000) -> str:
	"""Render an arbitrary completion object without allowing an unbounded repr."""

	try:
		rendered = repr(value)
	except Exception:
		rendered = f'<unrepresentable {type(value).__module__}.{type(value).__qualname__}>'
	return rendered[:max_characters]


def _safe_json_default(value: Any) -> Any:
	"""Represent non-JSON completion values with type and bounded text only."""

	model_dump = getattr(value, 'model_dump', None)
	if callable(model_dump):
		try:
			return model_dump(mode='json')
		except Exception:
			pass
	return {
		'type': f'{type(value).__module__}.{type(value).__qualname__}',
		'safe_repr': _safe_repr(value),
	}


def _truncate_head_tail(value: str, max_characters: int, marker: str) -> str:
	if len(value) <= max_characters:
		return value
	available = max_characters - len(marker)
	if available <= 0:
		return value[:max_characters]
	head = (available * 2) // 3
	tail = available - head
	return value[:head] + marker + value[-tail:]


def _invalid_decision_snapshot(completion: Any) -> str:
	"""Serialize one rejected completion into a bounded, model-readable snapshot."""

	try:
		rendered = json.dumps(
			completion,
			ensure_ascii=False,
			separators=(',', ':'),
			default=_safe_json_default,
		)
	except (TypeError, ValueError, OverflowError):
		rendered = json.dumps(
			{
				'type': f'{type(completion).__module__}.{type(completion).__qualname__}',
				'safe_repr': _safe_repr(completion),
			},
			ensure_ascii=False,
			separators=(',', ':'),
		)
	return _truncate_head_tail(
		rendered,
		_INVALID_DECISION_SNAPSHOT_MAX_CHARACTERS,
		_INVALID_DECISION_SNAPSHOT_TRUNCATION_MARKER,
	)


_MISSING_COMPLETION = object()


def _validation_error_input(error: ValidationError) -> Any:
	"""Return the rejected top-level value retained by a Pydantic error."""

	try:
		details = error.errors(include_url=False, include_context=False, include_input=True)
	except TypeError:  # pragma: no cover - compatibility with older Pydantic builds
		details = error.errors(include_url=False, include_context=False)
	for detail in details:
		if 'input' in detail:
			return detail['input']
	return _MISSING_COMPLETION


def _embedded_error_value(message: str) -> Any:
	"""Extract one ``input_value=...`` value from a provider error string.

	Pydantic's string rendering uses both JSON and Python-repr forms.  Decode only
	those literal forms; never evaluate arbitrary provider text.
	"""
	match = re.search(r'\binput_value\s*=\s*', message)
	if match is None:
		return _MISSING_COMPLETION

	value_text = message[match.end() :].lstrip()
	if not value_text:
		return _MISSING_COMPLETION

	try:
		value, end = json.JSONDecoder().raw_decode(value_text)
		return value
	except json.JSONDecodeError:
		pass

	# Find the end of a Python-repr container while respecting quoted strings.
	opening = value_text[0]
	if opening in '{[(':
		closing = {'{': '}', '[': ']', '(': ')'}
		stack = [opening]
		quote: str | None = None
		escaped = False
		for index, character in enumerate(value_text[1:], start=1):
			if quote is not None:
				if escaped:
					escaped = False
				elif character == '\\':
					escaped = True
				elif character == quote:
					quote = None
				continue
			if character in '\'"':
				quote = character
			elif character in closing:
				stack.append(character)
			elif character in closing.values():
				if not stack or character != closing[stack[-1]]:
					return _MISSING_COMPLETION
				stack.pop()
				if not stack:
					candidate = value_text[: index + 1]
					try:
						return ast.literal_eval(candidate)
					except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
						return _MISSING_COMPLETION

	# A scalar repr (most commonly a quoted string) ends before Pydantic's
	# ``input_type`` annotation.  Keep this fallback deliberately narrow.
	end_marker = re.search(r',\s*input_type\s*=', value_text)
	if end_marker is not None:
		candidate = value_text[: end_marker.start()].rstrip()
		try:
			return ast.literal_eval(candidate)
		except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
			return _MISSING_COMPLETION
	return _MISSING_COMPLETION


def _invalid_decision_snapshot_from_error(error: Exception) -> str | None:
	"""Recover a bounded rejected completion from parser/provider exceptions."""

	to_visit: list[tuple[BaseException, int]] = [(error, 0)]
	seen: set[int] = set()
	while to_visit:
		current, depth = to_visit.pop(0)
		if id(current) in seen or depth > 3:
			continue
		seen.add(id(current))

		if isinstance(current, ValidationError):
			completion = _validation_error_input(current)
			if completion is not _MISSING_COMPLETION:
				return _invalid_decision_snapshot(completion)

		raw_completion = getattr(current, 'raw_completion', None)
		if isinstance(raw_completion, str) and raw_completion:
			return _invalid_decision_snapshot(raw_completion)

		message = getattr(current, 'message', None)
		if not isinstance(message, str):
			message = str(current)
		completion = _embedded_error_value(message)
		if completion is not _MISSING_COMPLETION:
			return _invalid_decision_snapshot(completion)
		for related in (current.__cause__, current.__context__):
			if related is not None:
				to_visit.append((related, depth + 1))
	return None


def _coerce_agent_decision(
	completion: Any,
	*,
	source_service_group: str | None = None,
) -> AgentDecision:
	"""Validate an adapter completion even when it bypassed its output parser."""

	if isinstance(completion, AgentDecision):
		return completion
	if isinstance(completion, AgentDecisionEnvelope):
		return completion.decision
	if isinstance(completion, InitialPageAgentDecisionEnvelope):
		return completion.decision
	if isinstance(completion, RootUpdateRepairAgentDecisionEnvelope):
		return completion.decision
	try:
		return AgentDecision.model_validate(completion)
	except ValidationError as error:
		diagnostic = _raw_completion_contract_diagnostic(completion) or _validation_error_diagnostic(error)
		raise _InvalidStructuredDecision(
			diagnostic,
			_invalid_decision_snapshot(completion),
			source_service_group,
		) from error


@dataclass(slots=True)
class AgentRunOutcome:
	status: str
	agent_answer: str = ''
	evidence: list[str] = field(default_factory=list)
	evidence_records: list[dict[str, Any]] = field(default_factory=list)
	requirement_ledger: dict[str, Any] | None = None
	answer_candidate: dict[str, Any] | None = None
	completion_verification: dict[str, Any] | None = None
	completion_receipt: dict[str, Any] | None = None
	completion_feedback: str | None = None
	actions: list[str] = field(default_factory=list)
	thoughts: list[str] = field(default_factory=list)
	steps: list[dict[str, Any]] = field(default_factory=list)
	error: str | None = None
	duration_seconds: float = 0.0
	usage: dict[str, int] = field(default_factory=dict)
	verification: dict[str, object] = field(default_factory=dict)
	browser_failure: dict[str, Any] | None = None
	model_call_timing_summary: dict[str, int | float] = field(
		default_factory=lambda: dict(empty_model_call_timing_payload()['summary'])
	)


def _decision_action_payload(decision: AgentDecision) -> dict[str, Any]:
	payload = decision.action_payload()
	for field_name in ('thought', 'answer', 'evidence', 'success'):
		if decision.action != 'finish' or field_name == 'thought':
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


def _normalized_path_repair_diagnostic(
	path_action_result: PathJsonActionResult,
	*,
	path_tree: Mapping[str, Any],
) -> str:
	"""Return a bounded, model-independent correction for a rejected path delta.

	The previous decision can contain arbitrarily many invalid operations.  The
	next retry already receives the authoritative current tree, so replaying that
	decision is both unnecessary and likely to anchor the model on invalid IDs.
	"""

	reasons = tuple(operation.reason or '' for operation in path_action_result.operations)
	if path_action_result.blocked_reason == 'initial page exploration review requires at least one add operation':
		return 'The initial path review must include at least one `add` in `path_json_action.operations` to create a concrete exploration path before continuing.'
	if path_action_result.blocked_reason == 'initial page exploration review accepts add operations only':
		return 'The initial path review accepts only `add` operations in `path_json_action.operations`. Remove every `update` and retry.'
	if path_action_result.blocked_reason == 'initial page exploration review adds must use system initial root "1" as parent_path_id':
		return 'Every path created during the initial review must use `parent_path_id="1"` so it is a direct child of the system root.'
	if any('system initial root "1" is immutable' in reason for reason in reasons):
		return 'Remove every `update` whose `path_id` is `"1"`. Keep at least one `add`, and use `add` now to place every route visible on the current page that could lead to the answer into the path tree.'
	if any('marking a path failed requires' in reason for reason in reasons):
		return 'When marking a path `failed`, the same `update` must use `progress` to record concrete evidence that the path cannot reach the task destination or answer page. Then switch to a new exploration path.'
	if any('marking a path succeeded requires' in reason for reason in reasons):
		return 'When marking a path `succeeded`, the same `update` must use `progress` to record concrete evidence that the correct page or answer location has been reached.'
	if path_action_result.blocked_reason == 'exploration path tree is missing required system initial root "1"':
		return 'The exploration path tree is missing executor-created system root path `1`; this decision was not executed.'
	if any('path_id does not exist' in reason for reason in reasons):
		return 'A path update referenced a path that is absent from the current trusted tree. `update` may use only an existing `path_id`; use `add` for a new path.'
	if path_action_result.blocked and (path_action_result.blocked_reason or '').startswith('current_path_id does not exist'):
		return '`current_path_id` must be an existing non-terminal path in the current trusted tree. Select it again from the complete path tree.'
	if path_action_result.blocked and 'terminal' in (path_action_result.blocked_reason or ''):
		return '`current_path_id` cannot reference a terminal path. Select a non-terminal path from the complete path tree.'
	return 'One or more path deltas were not applied. Using the current trusted path tree and the `add`/`update` contract, submit only valid deltas for this decision.'


def _requires_root_update_repair_protocol(path_action_result: PathJsonActionResult) -> bool:
	"""Whether the next same-step retry must hide the ``update`` operation.

	Only the executor's immutable-system-root rejection activates this temporary
	protocol. Other path errors retain the normal schema because a concrete
	path's verified lifecycle update may still be required to repair them.
	"""

	return any(
		operation.requested_op == 'update'
		and 'system initial root "1" is immutable' in (operation.reason or '')
		for operation in path_action_result.operations
	)


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


def _is_closed_browser_runtime_result(result: WebRetrieverActionResult | str) -> bool:
	"""Recognize a runtime action result that needs session re-grounding."""

	return isinstance(result, WebRetrieverActionResult) and (
		result.error_type == 'BrowserSessionClosed' or is_browser_session_closed_error(result.error)
	)


class _RefundableStepCounter:
	"""Iterator that can give a closed-target recovery its step back once."""

	def __init__(self, maximum: int) -> None:
		self._maximum = maximum
		self._next = 0
		self._last: int | None = None

	def __iter__(self) -> _RefundableStepCounter:
		return self

	def __next__(self) -> int:
		if self._next >= self._maximum:
			raise StopIteration
		self._last = self._next
		self._next += 1
		return self._last

	def refund_last(self) -> None:
		if self._last is None or self._next != self._last + 1:
			raise RuntimeError('Only the current step may be refunded')
		self._next = self._last


def _observation_hash(rendered_observation: str) -> str:
	"""Stable browser-state identity used only for exact-action loop protection."""

	return hashlib.sha256(rendered_observation.encode('utf-8')).hexdigest()


# Actions whose whole purpose is to change the viewport or the browsing position.
# Repeating them with different parameters is normal exploration; alternating
# between two of them over the same states is a stall.
_NAVIGATION_ACTIONS = frozenset({'scroll', 'back', 'navigate', 'click_xy', 'switch_tab', 'drag'})
# Read-only probes: many of these in a row without any state change means the
# current modality is exhausted, no matter how the query string varies.
_PROBE_ACTIONS = frozenset({'find_text', 'read_element', 'find_chart_data_requests'})


def _action_intent(decision: AgentDecision) -> str:
	"""Coarse action identity that ignores incidental parameter churn.

	Case 62 re-submitted the same form through five different ``element_id``
	values, and case 95 dragged the same date-picker column ten times with
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


def _record_exploration_decision(
	tracker: ExplorationPathTracker,
	*,
	decision: AgentDecision,
) -> None:
	"""Record a decision summary after a completed action."""
	if tracker.answer_priority_mode:
		return
	tracker.record_decision(current_path_id=decision.current_path_id, decision_summary=decision.decision_summary)


def _usage_dict(usage: Any) -> dict[str, int]:
	if usage is None:
		return {}
	if hasattr(usage, 'model_dump'):
		raw = usage.model_dump(exclude_none=True)
	else:
		raw = vars(usage) if hasattr(usage, '__dict__') else {}
	return {str(key): int(value) for key, value in raw.items() if isinstance(value, int)}


def _is_non_retryable_analysis_status(payload: Any) -> bool:
	return isinstance(payload, Mapping) and payload.get('status') in _NON_RETRYABLE_ANALYSIS_STATUSES


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
	if not image_bytes:
		return
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
		completion_gate: CompletionGate | None = None,
		cancellation: CancellationToken | None = None,
		observer: RunObserver | None = None,
		worker_id: int | None = None,
		task_deadline_monotonic: float | None = None,
		recover_unstarted_runtime: Callable[[], Awaitable[Any]] | None = None,
		recover_missing_task_page: Callable[[], Awaitable[Any]] | None = None,
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
		self._model_output_protocol_variants = {
			'standard': _model_output_protocol(llm, AgentDecisionEnvelope),
			'initial_page_add_only': _model_output_protocol(llm, InitialPageAgentDecisionEnvelope),
			'root_update_repair_add_only': _model_output_protocol(llm, RootUpdateRepairAgentDecisionEnvelope),
		}
		# Preserve the long-standing top-level protocol entry for downstream log
		# readers; each request records the chosen variant below.
		self._model_output_protocol = self._model_output_protocol_variants['standard']
		self._model_prompt_log_path = self.task_dir / MODEL_PROMPT_LOG_FILENAME
		self._model_prompt_log: dict[str, Any] = {}
		self._model_call_timing_path = self.task_dir / MODEL_CALL_TIMING_FILENAME
		self._model_call_timing: dict[str, Any] = empty_model_call_timing_payload()
		self._model_service_event_start = llm.event_count if isinstance(llm, ModelServiceRouter) else 0
		self.task_deadline_monotonic = task_deadline_monotonic
		self._recover_unstarted_runtime = recover_unstarted_runtime
		self._recover_missing_task_page = recover_missing_task_page or recover_unstarted_runtime
		self._task_page_recovery_stages: list[dict[str, Any]] = []
		self._trusted_data_manifests: dict[str, str] = {}
		self._ready_data_dirs: set[str] = set()
		self._unavailable_analysis_data_dirs: set[str] = set()
		self._analysis_not_ready_recovery_ready_dirs: frozenset[str] | None = None
		self._data_artifact_filters: dict[str, dict[str, Any]] = {}
		self._announced_data_artifact_ids: set[str] = set()
		self._announced_download_timeout_keys: set[str] = set()
		self.chart_network_inspector = chart_network_inspector or ChartNetworkInspector(
			llm,
			affinity_key=self.task.task_id,
			# Leave room inside the 300-second task watchdog for normalization,
			# the 90-second analysis action, and the final answer.
			model_timeout_seconds=min(_FIND_CHART_MAX_SECONDS, model_timeout_seconds),
		)
		# Import the optional PandasAI runtime only if the model actually selects
		# its action.  Ordinary browser tasks and unit tests therefore do not pay
		# its import/dependency cost.
		self.data_analysis_assistant = data_analysis_assistant
		self.completion_gate = completion_gate or CompletionGate(
			task=self.task,
			task_dir=self.task_dir,
			reviewer=LLMCompletionReviewer(
				llm=self.llm,
				task_dir=self.task_dir,
				task_deadline_monotonic=self.task_deadline_monotonic,
				model_timeout_seconds=self.model_timeout_seconds,
				affinity_key=self.task.task_id,
			),
		)
		self.cancellation = cancellation or CancellationToken()
		self.observer = observer or NoOpRunObserver()
		self.worker_id = worker_id
		self._completion_usage_accounted: dict[str, int] = {}
		self._completion_enabled = _supports_completion_protocol(llm)
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
				affinity_key=self.task.task_id,
				model_timeout_seconds=min(90.0, self.model_timeout_seconds),
				max_output_chars=32_000,
				trusted_manifest_hashes=self._trusted_data_manifests,
			)
		return self.data_analysis_assistant

	def _remaining_task_seconds(self) -> float:
		if self.task_deadline_monotonic is None:
			return float('inf')
		return max(0.0, self.task_deadline_monotonic - time.monotonic())

	async def _emit(self, event_type: str, *, payload: Mapping[str, Any]) -> None:
		await emit_safely(
			self.observer,
			RunnerEvent(
				type=event_type,
				worker_id=self.worker_id,
				task_id=self.task.task_id,
				task_idx=self.task.task_idx,
				payload=payload,
			),
			logger=logging.getLogger('webretriever.agent'),
		)

	async def _emit_completed_step(self, *, step: int, action: str, outcome: str, thought: str) -> None:
		await self._emit(
			'task.step.completed',
			payload={
				'step': step,
				'max_steps': self.max_steps,
				'action': action,
				'outcome': outcome,
				'thought': thought,
			},
		)

	async def _emit_decided_step(self, *, step: int, action: str, thought: str) -> None:
		await self._emit(
			'task.step.decided',
			payload={
				'step': step,
				'max_steps': self.max_steps,
				'action': action,
				'thought': thought,
			},
		)

	def _sync_completion_outcome(self, outcome: AgentRunOutcome) -> None:
		"""Copy executor-owned completion data into the evaluator-facing outcome."""

		snapshot = self.completion_gate.snapshot()
		outcome.requirement_ledger = snapshot.get('requirement_ledger')
		outcome.answer_candidate = snapshot.get('answer_candidate')
		outcome.evidence = list(snapshot.get('evidence') or [])
		outcome.evidence_records = list(snapshot.get('evidence_records') or [])
		outcome.completion_verification = snapshot.get('completion_verification')
		outcome.completion_receipt = snapshot.get('completion_receipt')
		outcome.completion_feedback = snapshot.get('completion_feedback')
		usage = getattr(self.completion_gate, 'usage', {})
		if isinstance(usage, Mapping):
			for key, total in usage.items():
				if type(total) is not int:
					continue
				accounted = self._completion_usage_accounted.get(str(key), 0)
				if total > accounted:
					outcome.usage[str(key)] = outcome.usage.get(str(key), 0) + total - accounted
				self._completion_usage_accounted[str(key)] = total

	async def _await_runtime_recovery(self, awaitable: Awaitable[Any]) -> Any:
		"""Await a recovery operation without extending the task deadline."""

		remaining = self._remaining_task_seconds()
		if remaining <= 0:
			raise TimeoutError('task deadline expired before browser recovery')
		if self.task_deadline_monotonic is None:
			return await awaitable
		return await _await_with_hard_timeout(awaitable, remaining)

	@staticmethod
	def _is_runtime_not_started_error(error: BaseException) -> bool:
		return _RUNTIME_NOT_STARTED_ERROR in str(error).casefold()

	async def _recover_initially_unstarted_runtime(self) -> tuple[bool, bool]:
		"""Restart once locally, then ask the runner for one clean worker runtime."""

		attempted = False
		start = getattr(self.runtime, 'start', None)
		if callable(start):
			attempted = True
			try:
				await self._await_runtime_recovery(start(self.task.website))
			except TimeoutError:
				raise
			except Exception:
				pass
			else:
				return True, attempted

		if self._recover_unstarted_runtime is None:
			return False, attempted
		attempted = True
		try:
			replacement_runtime = await self._await_runtime_recovery(self._recover_unstarted_runtime())
		except TimeoutError:
			raise
		except Exception:
			return False, attempted
		if replacement_runtime is None:
			return False, attempted
		self.runtime = replacement_runtime
		return True, attempted

	@staticmethod
	def _is_task_page_unavailable_error(error: BaseException) -> bool:
		message = str(error).casefold()
		return (
			'browserruntime has no active task page' in message
			or 'browserruntime active task page is closed' in message
		)

	@staticmethod
	def _task_page_state(runtime: Any) -> str | None:
		"""Return a small runtime-independent state hint for failure taxonomy."""

		page = getattr(runtime, 'page', None)
		if page is None:
			return 'missing'
		try:
			if page.is_closed():
				return 'closed'
		except Exception:
			return None
		return 'active'

	async def _await_bounded_recovery(self, awaitable: Awaitable[Any], maximum_seconds: float) -> Any:
		"""Await one recovery stage without extending the task deadline."""

		remaining = self._remaining_task_seconds()
		if remaining <= 0:
			raise TimeoutError('task deadline expired before browser recovery')
		return await _await_with_hard_timeout(awaitable, min(maximum_seconds, remaining))

	async def _recover_task_page(self, *, skip_same_context: bool = False) -> tuple[bool, bool]:
		"""Recreate a missing task page, then escalate to a clean worker once."""

		attempted = False
		restart = getattr(self.runtime, 'restart_task_page', None)
		if not skip_same_context and callable(restart):
			attempted = True
			started_at = time.monotonic()
			try:
				await self._await_bounded_recovery(
					restart(self.task.website, timeout_seconds=_TASK_PAGE_RESTART_MAX_SECONDS),
					_TASK_PAGE_RESTART_MAX_SECONDS,
				)
			except TimeoutError as exc:
				self._task_page_recovery_stages.append(
					{
						'stage': 'same_context_page',
						'status': 'timeout',
						'elapsed_seconds': round(time.monotonic() - started_at, 3),
						'error': _recovery_error_text(exc),
					}
				)
				if self._remaining_task_seconds() <= 0:
					raise
			except Exception as exc:
				self._task_page_recovery_stages.append(
					{
						'stage': 'same_context_page',
						'status': 'failed',
						'elapsed_seconds': round(time.monotonic() - started_at, 3),
						'error': _recovery_error_text(exc),
					}
				)
			else:
				self._task_page_recovery_stages.append(
					{
						'stage': 'same_context_page',
						'status': 'recovered',
						'elapsed_seconds': round(time.monotonic() - started_at, 3),
					}
				)
				return True, attempted

		if self._recover_missing_task_page is None:
			return False, attempted
		attempted = True
		started_at = time.monotonic()
		try:
			replacement_runtime = await self._await_bounded_recovery(
				self._recover_missing_task_page(),
				_CLEAN_WORKER_RECOVERY_MAX_SECONDS,
			)
		except TimeoutError as exc:
			self._task_page_recovery_stages.append(
				{
					'stage': 'clean_cdp_worker',
					'status': 'timeout',
					'elapsed_seconds': round(time.monotonic() - started_at, 3),
					'error': _recovery_error_text(exc),
				}
			)
			if self._remaining_task_seconds() <= 0:
				raise
			return False, attempted
		except Exception as exc:
			self._task_page_recovery_stages.append(
				{
					'stage': 'clean_cdp_worker',
					'status': 'failed',
					'elapsed_seconds': round(time.monotonic() - started_at, 3),
					'error': _recovery_error_text(exc),
				}
			)
			return False, attempted
		if replacement_runtime is None:
			self._task_page_recovery_stages.append(
				{
					'stage': 'clean_cdp_worker',
					'status': 'failed',
					'elapsed_seconds': round(time.monotonic() - started_at, 3),
					'error': 'recovery callback returned no runtime',
				}
			)
			return False, attempted
		self.runtime = replacement_runtime
		self._task_page_recovery_stages.append(
			{
				'stage': 'clean_cdp_worker',
				'status': 'recovered',
				'elapsed_seconds': round(time.monotonic() - started_at, 3),
			}
		)
		return True, attempted

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

	def _ready_data_dir_key(self, data_dir: str | None) -> str | None:
		if not isinstance(data_dir, str):
			return None
		try:
			key = str(Path(data_dir).resolve(strict=True))
		except (FileNotFoundError, OSError):
			return None
		return key if key in self._ready_data_dirs else None

	def _eligible_data_dirs(self) -> tuple[str, ...]:
		"""Return the complete task-local analysis inputs that remain callable."""

		return tuple(sorted(self._ready_data_dirs - self._unavailable_analysis_data_dirs))

	def _activate_analysis_not_ready_recovery(self) -> None:
		"""Latch document-evidence recovery until a newly observed eligible directory exists."""

		if self._analysis_not_ready_recovery_ready_dirs is None:
			self._analysis_not_ready_recovery_ready_dirs = frozenset(self._ready_data_dirs)

	def _analysis_not_ready_recovery_notice(self) -> str:
		baseline = self._analysis_not_ready_recovery_ready_dirs
		if baseline is None:
			return ''
		if any(data_dir not in baseline for data_dir in self._eligible_data_dirs()):
			self._analysis_not_ready_recovery_ready_dirs = None
			return ''
		return _ANALYSIS_NOT_READY_RECOVERY

	def _is_ready_data_dir(self, data_dir: str | None) -> bool:
		return self._ready_data_dir_key(data_dir) is not None

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

		if not self.structured_prompt_log:
			self._model_prompt_log = {
				'format': MODEL_PROMPT_LOG_FORMAT,
				'system_prompt': prompt_text_lines(self.system_prompt),
				'output_protocol': self._model_output_protocol,
				'output_protocol_variants': self._model_output_protocol_variants,
				'steps': [],
			}
			atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)
			return

		self._model_prompt_log = {
			'format': STRUCTURED_MODEL_PROMPT_LOG_FORMAT,
			'metadata': model_prompt_log_metadata(),
			'task': self.task.prompt_payload(),
			'output_protocol': self._model_output_protocol,
			'output_protocol_variants': self._model_output_protocol_variants,
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
		self._refresh_model_service_routing()
		atomic_write_json(self._model_call_timing_path, self._model_call_timing)

	@property
	def model_call_timing_payload(self) -> dict[str, Any]:
		"""Return the current model-call table for the runner's final artifact write."""

		self._refresh_model_service_routing()
		return self._model_call_timing

	def _refresh_model_service_routing(self) -> None:
		if not isinstance(self.llm, ModelServiceRouter):
			return
		self._model_call_timing['service_routing'] = {
			'attempts': self.llm.service_events_since(self._model_service_event_start),
			'services': self.llm.service_state_payload(),
		}

	def _update_timing_summary(self, outcome: AgentRunOutcome) -> None:
		self._refresh_model_service_routing()
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

	def _record_model_attempt_started(self, *, step: int, attempt: int, service_name: str | None = None) -> None:
		"""Durably record a model request immediately before it is submitted.

		Wall-clock time is used only for the human-readable audit trail; elapsed
		time accounting continues to use ``time.monotonic()``.  Persist before the
		await so a task watchdog cannot erase evidence of an in-flight request.
		"""

		entry = self._timing_step(step)
		attempts = entry['attempts']
		if not isinstance(attempts, list):
			raise TypeError('model call timing attempts must be a list')
		if attempts and attempts[-1].get('status') == 'in_progress':
			raise ValueError('cannot start a model attempt before the previous attempt finishes')
		request_started_at = datetime.now(timezone.utc).isoformat()
		attempts.append(
			{
				'attempt': attempt,
				'request_started_at': request_started_at,
				'status': 'in_progress',
				'model_wait_seconds': 0.0,
				'retry_wait_seconds': 0.0,
				'total_wait_seconds': 0.0,
			}
		)
		if service_name is not None:
			attempts[-1]['service'] = service_name
		entry.setdefault('request_started_at', request_started_at)
		atomic_write_json(self._model_call_timing_path, self._model_call_timing)

	def _record_model_attempt(
		self,
		*,
		step: int,
		attempt: int,
		service_name: str | None = None,
		status: str,
		wait_seconds: float,
		error: str | None = None,
	) -> None:
		"""Persist one completed model wait before any reconnection delay."""

		entry = self._timing_step(step)
		attempts = entry['attempts']
		if not isinstance(attempts, list):
			raise TypeError('model call timing attempts must be a list')
		if (
			not attempts
			or attempts[-1].get('attempt') != attempt
			or attempts[-1].get('status') != 'in_progress'
		):
			raise ValueError('completed model attempt has no matching started attempt')
		attempt_entry = attempts[-1]
		attempt_entry['status'] = status
		if service_name is not None:
			attempt_entry['service'] = service_name
		attempt_entry['model_wait_seconds'] = round(max(0.0, wait_seconds), 3)
		attempt_entry['retry_wait_seconds'] = 0.0
		attempt_entry['total_wait_seconds'] = round(max(0.0, wait_seconds), 3)
		if error:
			attempt_entry['error'] = error[:1_000]
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
		output_format: type[BaseModel],
		excluded_service_groups: Collection[str] = (),
		service_metadata: dict[str, str] | None = None,
	) -> Any:
		"""Call the decision model through the configured service strategy.

		A ``ModelServiceRouter`` picks the globally least-loaded eligible service
		for every attempt and recovers provider failures until this task's deadline.
		Legacy direct model instances retain the existing bounded reconnect retry
		behavior. Each underlying OpenAI client is configured with zero retries so
		there is no hidden retry loop inside either strategy.
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

		def record_attempt_started(attempt: int) -> None:
			self._record_model_attempt_started(step=step, attempt=attempt)
			self._update_timing_summary(outcome)

		def record_retry_wait(attempt: int, wait_seconds: float) -> None:
			self._record_retry_wait(step=step, attempt=attempt, wait_seconds=wait_seconds)
			self._update_timing_summary(outcome)

		def record_service_attempt_started(attempt: int, service_name: str) -> None:
			if service_metadata is not None:
				service_metadata['service_name'] = service_name
				service_group = self.llm.service_group_for_name(service_name) if isinstance(self.llm, ModelServiceRouter) else None
				if service_group is not None:
					service_metadata['service_group'] = service_group
			self._record_model_attempt_started(step=step, attempt=attempt, service_name=service_name)
			self._update_timing_summary(outcome)

		def record_service_attempt(
			attempt: int,
			service_name: str,
			status: str,
			wait_seconds: float,
			error: Exception | None,
		) -> None:
			if service_metadata is not None:
				service_metadata['service_name'] = service_name
				service_group = self.llm.service_group_for_name(service_name) if isinstance(self.llm, ModelServiceRouter) else None
				if service_group is not None:
					service_metadata['service_group'] = service_group
			self._record_model_attempt(
				step=step,
				attempt=attempt,
				service_name=service_name,
				status=status,
				wait_seconds=wait_seconds,
				error=f'{type(error).__name__}: {error}' if error is not None else None,
			)
			self._update_timing_summary(outcome)

		if isinstance(self.llm, ModelServiceRouter):
			return await invoke_with_service_failover(
				self.llm,
				lambda client: client.ainvoke(messages, output_format=output_format),
				timeout_seconds=lambda: min(self.model_timeout_seconds, self._remaining_task_seconds()),
				on_attempt_started=record_service_attempt_started,
				on_attempt_finished=record_service_attempt,
				affinity_key=self.task.task_id,
				excluded_service_groups=excluded_service_groups,
				structured_output=True,
			)

		return await invoke_with_reconnect_retries(
			lambda: self.llm.ainvoke(messages, output_format=output_format),
			timeout_seconds=lambda: min(self.model_timeout_seconds, self._remaining_task_seconds()),
			on_attempt_started=record_attempt_started,
			on_attempt_finished=record_attempt,
			on_retry_wait_finished=record_retry_wait,
			structured_output=True,
		)

	def _record_model_prompt(
		self,
		*,
		step: int,
		prompt_document: PromptDocument,
		screenshot_path: Path | None,
		output_protocol_variant: str,
		analysis_capability: Mapping[str, Any],
		hidden_actions: Collection[str] = (),
		excluded_service_groups: Collection[str] = (),
	) -> int:
		"""Atomically persist one model request before it is submitted.

		When available, the screenshot is retained in ``trajectory`` and referenced
		here rather than embedded. A temporarily unavailable screenshot is recorded
		as ``null`` so the log still matches the actual text-only model request.
		"""

		steps = self._model_prompt_log['steps']
		if not isinstance(steps, list):  # Defensive guard for future format edits.
			raise TypeError('model prompt log steps must be a list')
		image = (
			{
				'media_type': 'image/png',
				'detail': 'high',
				'path': str(screenshot_path.relative_to(self.task_dir)),
			}
			if screenshot_path is not None
			else None
		)
		prompt_index = len(steps)
		if not self.structured_prompt_log:
			steps.append(
				{
					'step': step + 1,
					'prompt': prompt_text_lines(prompt_document.text),
					'image': image,
					'output_protocol_variant': output_protocol_variant,
					'analysis_capability': dict(analysis_capability),
				}
			)
		else:
			steps.append(
				{
					'step': step + 1,
					'output_protocol_variant': output_protocol_variant,
					'prompt': {
						'role': prompt_document.role,
						'rendered_text': prompt_document.text,
						'sections': [dict(section) for section in prompt_document.sections],
						'metrics': dict(prompt_document.metrics),
					},
					'image': image,
					'analysis_capability': dict(analysis_capability),
				}
			)
		if excluded_service_groups or hidden_actions:
			entry = steps[-1]
			if not isinstance(entry, dict):
				raise TypeError('model prompt log step must be an object')
			if excluded_service_groups:
				entry['excluded_model_service_groups'] = sorted(set(excluded_service_groups))
			if hidden_actions:
				entry['temporarily_hidden_actions'] = sorted(set(hidden_actions))
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)
		return prompt_index

	@staticmethod
	def _decision_output_format(
		context: StepContext,
		*,
		eligible_data_dirs: tuple[str, ...] | None = None,
		root_update_repair: bool = False,
		hidden_actions: Collection[str] = (),
	) -> type[BaseModel]:
		"""Select the provider schema matching this observation's path state."""

		review = context.exploration_review
		initial_page = not context.answer_priority_mode and review is not None and review.trigger == 'initial_page'
		normalized_hidden_actions = tuple(
			sorted(
				action
				for action in set(hidden_actions)
				if action in ACTION_PARAMETER_CONTRACTS and action not in {'finish', 'submit_answer_candidate'}
			)
		)
		if eligible_data_dirs is None and not normalized_hidden_actions:
			if initial_page:
				return InitialPageAgentDecisionEnvelope
			return RootUpdateRepairAgentDecisionEnvelope if root_update_repair else AgentDecisionEnvelope
		return _capability_gated_output_format(
			initial_page=initial_page,
			root_update_repair=root_update_repair,
			eligible_data_dirs=eligible_data_dirs or (),
			hidden_actions=normalized_hidden_actions,
		)

	@staticmethod
	def _output_protocol_variant(output_format: type[BaseModel]) -> str:
		decision_model = getattr(output_format, '__structured_decision_model__', None)
		if decision_model is InitialPageAgentDecisionEnvelope.__structured_decision_model__:
			return 'initial_page_add_only'
		if decision_model is RootUpdateRepairAgentDecisionEnvelope.__structured_decision_model__:
			return 'root_update_repair_add_only'
		return 'standard'

	def _record_model_result(
		self,
		*,
		step: int,
		prompt_index: int | None = None,
		started_at: float,
		usage: Mapping[str, int] | None = None,
		error: str | None = None,
		raw_completion: str | None | object = _MISSING_COMPLETION,
	) -> None:
		"""Add call results after the already-durable structured model input."""

		steps = self._model_prompt_log.get('steps')
		resolved_prompt_index = step if prompt_index is None else prompt_index
		if not isinstance(steps, list) or resolved_prompt_index < 0 or resolved_prompt_index >= len(steps):
			raise ValueError('model prompt result has no matching persisted input')
		entry = steps[resolved_prompt_index]
		if not isinstance(entry, dict):
			raise TypeError('structured model prompt step must be an object')
		if raw_completion is not _MISSING_COMPLETION:
			entry['model_response'] = raw_completion
		if self.structured_prompt_log:
			entry['model_call'] = {
				'duration_seconds': round(max(0.0, time.monotonic() - started_at), 3),
				'usage': dict(usage or {}),
				'error': error,
			}
		elif raw_completion is _MISSING_COMPLETION:
			return
		atomic_write_json(self._model_prompt_log_path, self._model_prompt_log)

	async def _request_model_decision(
		self,
		*,
		step: int,
		context: StepContext,
		screenshot: bytes,
		raw_path: Path | None,
		outcome: AgentRunOutcome,
		excluded_service_groups: Collection[str] = (),
		root_update_repair: bool = False,
		hidden_actions: Collection[str] = (),
	) -> tuple[AgentDecision, dict[str, int], int, float]:
		"""Request one decision while allowing path-tree retries to reuse a step.

		The caller may invoke this more than once for the same ``step`` when the
		model's path-tree delta is not executable.  Prompt-log entries are therefore
		indexed by their append position rather than by the competition step number.
		"""

		prompt_document = self.prompt_composer.compose_step(context)
		content: list[ContentPartTextParam | ContentPartImageParam] = [
			ContentPartTextParam(text=prompt_document.text),
		]
		if screenshot:
			content.append(
				ContentPartImageParam(
					image_url=ImageURL(
						url=f'data:image/png;base64,{base64.b64encode(screenshot).decode("ascii")}',
						detail='high',
					)
				)
			)
		eligible_data_dirs = self._eligible_data_dirs()
		normalized_hidden_actions = tuple(
			sorted(
				action
				for action in set(hidden_actions)
				if action in ACTION_PARAMETER_CONTRACTS and action not in {'finish', 'submit_answer_candidate'}
			)
		)
		action_contracts = _action_contracts_for_data_capability(
			eligible_data_dirs,
			hidden_actions=normalized_hidden_actions,
		)
		system_document = self.prompt_composer.system_for_action_contracts(action_contracts)
		messages = [
			SystemMessage(content=system_document.text),
			UserMessage(content=content),
		]
		model_call_timeout = min(self.model_timeout_seconds, self._remaining_task_seconds())
		if model_call_timeout <= 0:
			raise _DecisionTaskDeadline()
		output_format = self._decision_output_format(
			context,
			eligible_data_dirs=eligible_data_dirs,
			root_update_repair=root_update_repair,
			hidden_actions=normalized_hidden_actions,
		)
		analysis_capability = {
			'status': 'available' if eligible_data_dirs else 'unavailable',
			'eligible_data_dirs': list(eligible_data_dirs),
		}
		service_metadata: dict[str, str] = {}
		prompt_index = self._record_model_prompt(
			step=step,
			prompt_document=prompt_document,
			screenshot_path=raw_path,
			output_protocol_variant=self._output_protocol_variant(output_format),
			analysis_capability=analysis_capability,
			hidden_actions=normalized_hidden_actions,
			excluded_service_groups=excluded_service_groups,
		)
		model_call_started_at = time.monotonic()
		try:
			response = await self._invoke_decision_with_retries(
				messages=messages,
				step=step,
				outcome=outcome,
				output_format=output_format,
				excluded_service_groups=excluded_service_groups,
				service_metadata=service_metadata,
			)
		except TimeoutError as exc:
			if isinstance(self.llm, ModelServiceRouter) and str(exc):
				timeout_error = str(exc)
			else:
				timeout_error = (
					f'Model request exceeded {model_call_timeout:g} seconds after up to '
					f'{MODEL_RETRY_MAX_ATTEMPTS} connection attempts'
				)
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				raw_completion=None,
				error=timeout_error,
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
					raw_completion=getattr(exc, 'raw_completion', None),
				)
				source_service_group = getattr(exc, 'service_group', None)
				if not isinstance(source_service_group, str) or not source_service_group.strip():
					source_service_group = service_metadata.get('service_group')
				if not isinstance(source_service_group, str) or not source_service_group.strip():
					source_service_group = None
				raise _InvalidStructuredDecision(
					diagnostic,
					_invalid_decision_snapshot_from_error(exc),
					source_service_group,
				) from exc
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				error=f'{type(exc).__name__}: {exc}',
				raw_completion=getattr(exc, 'raw_completion', None),
			)
			raise

		model_usage = _usage_dict(getattr(response, 'usage', None))
		_merge_usage(outcome.usage, model_usage)
		completion = getattr(response, 'completion', _MISSING_COMPLETION)
		raw_completion = getattr(response, 'raw_completion', None)
		source_service_group = service_metadata.get('service_group')
		if not isinstance(source_service_group, str) or not source_service_group.strip():
			source_service_group = None
		try:
			decision = _coerce_agent_decision(
				completion,
				source_service_group=source_service_group,
			)
			if decision.action in normalized_hidden_actions:
				raise _InvalidStructuredDecision(
					f"action {decision.action!r} is temporarily unavailable for the current same-step repair",
					_invalid_decision_snapshot(raw_completion),
					source_service_group,
				)
		except _InvalidStructuredDecision as exc:
			model_error = f'Model structured decision is invalid: {exc.diagnostic}'
			self._record_model_result(
				step=step,
				prompt_index=prompt_index,
				started_at=model_call_started_at,
				usage=model_usage,
				error=model_error,
				raw_completion=raw_completion,
			)
			raise
		self._record_model_result(
			step=step,
			prompt_index=prompt_index,
			started_at=model_call_started_at,
			usage=model_usage,
			raw_completion=raw_completion,
		)
		return decision, model_usage, prompt_index, model_call_started_at

	async def run(self) -> AgentRunOutcome:
		started_at = time.monotonic()
		outcome = AgentRunOutcome(status='FAIL')
		self._partial_outcome = outcome
		self._reset_model_prompt_log()
		self._reset_model_call_timing()
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

		def cancellation_requested() -> bool:
			if not self.cancellation.cancelled:
				return False
			outcome.status = 'FAIL_CANCELLED'
			outcome.error = self.cancellation.reason
			return True

		if self._completion_enabled:
			try:
				await self.completion_gate.prepare()
			except RequirementLedgerError as exc:
				outcome.status = 'FAIL_REQUIREMENT_LEDGER'
				outcome.error = str(exc)
				self._sync_completion_outcome(outcome)
				outcome.duration_seconds = round(time.monotonic() - started_at, 3)
				outcome.verification = verification.summary()
				self._update_timing_summary(outcome)
				return outcome
			else:
				self._sync_completion_outcome(outcome)

		# A stale semantic click may consume one explicitly bounded recovery action.
		# The regular loop guard below keeps the additional slot unavailable unless
		# the immediately prior terminal-threshold failure qualified for recovery.
		step_counter = _RefundableStepCounter(self.max_steps + 1)
		stale_click_recovery_pending = False
		runtime_not_started_recovery_attempted = False
		task_page_recovery_attempted = False
		task_page_same_context_recovered = False
		for step in step_counter:
			if cancellation_requested():
				break
			if step >= self.max_steps and not stale_click_recovery_pending:
				outcome.status = 'FAIL_MAX_STEPS'
				outcome.error = f'Reached the competition limit of {self.max_steps} steps without a final answer'
				break
			stale_click_recovery_step = stale_click_recovery_pending
			try:
				await self._emit('task.phase_changed', payload={'phase': 'observing'})
				if cancellation_requested():
					break
				observation = await self.runtime.observe(step)
			except Exception as exc:
				if cancellation_requested():
					break
				task_page_state = self._task_page_state(self.runtime)
				session_closed = is_browser_session_closed_error(exc)
				if session_closed:
					try:
						recovered = await self.runtime.recover_live_task_page()
					except Exception:
						recovered = False
					if recovered:
						# The old observation and any action-failure streak refer to a
						# page that is no longer trustworthy.  Re-grounding is a zero-
						# quota recovery, so discard those guards before observing the
						# surviving page again.
						consecutive_errors = 0
						last_action_signature = None
						repeated_action_count = 0
						recent_signatures.clear()
						no_change_counts.clear()
						blocked_no_change_signatures.clear()
						blocked_loop_intents.clear()
						step_counter.refund_last()
						last_outcome = 'Browser target closed; a surviving task page was re-grounded. Observe it before deciding again.'
						continue
				if self._is_task_page_unavailable_error(exc):
					# A missing active-page pointer can coexist with another live page
					# already owned by this task.  Re-ground on that page before changing
					# navigation state or replacing the worker.
					try:
						recovered = await self.runtime.recover_live_task_page()
					except Exception:
						recovered = False
					if recovered:
						consecutive_errors = 0
						last_action_signature = None
						repeated_action_count = 0
						recent_signatures.clear()
						no_change_counts.clear()
						blocked_no_change_signatures.clear()
						blocked_loop_intents.clear()
						step_counter.refund_last()
						last_outcome = _SURVIVING_TASK_PAGE_RECOVERY_NOTICE
						continue
					if not task_page_recovery_attempted or task_page_same_context_recovered:
						recovered, recovery_attempted = await self._recover_task_page(
							skip_same_context=task_page_same_context_recovered,
						)
						task_page_recovery_attempted = task_page_recovery_attempted or recovery_attempted
						if task_page_same_context_recovered:
							# The clean-worker stage is single-use, regardless of whether it
							# returns a runtime or fails.
							task_page_same_context_recovered = False
						if recovered:
							task_page_same_context_recovered = bool(
								self._task_page_recovery_stages
								and self._task_page_recovery_stages[-1]['stage'] == 'same_context_page'
								and self._task_page_recovery_stages[-1]['status'] == 'recovered'
							)
							# The rebuilt page has no valid element bindings or loop guards from
							# the lost page.  Keep completed action records intact: their effects
							# may be externally visible and must never be replayed automatically.
							consecutive_errors = 0
							last_action_signature = None
							repeated_action_count = 0
							recent_signatures.clear()
							no_change_counts.clear()
							blocked_no_change_signatures.clear()
							blocked_loop_intents.clear()
							step_counter.refund_last()
							last_outcome = _TASK_PAGE_RECOVERY_NOTICE
							continue
				if (
					step == 0
					and not outcome.steps
					and not runtime_not_started_recovery_attempted
					and self._is_runtime_not_started_error(exc)
				):
					recovered, recovery_attempted = await self._recover_initially_unstarted_runtime()
					runtime_not_started_recovery_attempted = recovery_attempted
					if recovered:
						step_counter.refund_last()
						last_outcome = _RUNTIME_NOT_STARTED_RECOVERY_NOTICE
						continue
				browser_failure = classify_browser_failure(
					exc,
					phase=BrowserFailurePhase.OBSERVATION,
					session_closed=session_closed,
					task_page_state=task_page_state,
					recovery_exhausted=task_page_recovery_attempted
					and not any(stage.get('status') == 'recovered' for stage in self._task_page_recovery_stages),
				)
				outcome.status = browser_failure.status
				outcome.error = f'Observation failed: {type(exc).__name__}: {exc}'
				failure_payload = browser_failure.payload(
					recovery_attempted=session_closed or runtime_not_started_recovery_attempted
				)
				if task_page_recovery_attempted:
					failure_payload['recovery_attempted'] = True
					failure_payload['recovery_stages'] = list(self._task_page_recovery_stages)
				outcome.browser_failure = failure_payload
				break
			if cancellation_requested():
				break
			try:
				exploration_tracker.ensure_system_initial_path(start_url=observation.url)
			except ExplorationPathError as exc:
				outcome.status = 'FAIL_EXPLORATION_PATH_INITIALIZATION'
				outcome.error = f'Initial exploration-path setup failed: {exc}'
				break

			screenshot = observation.screenshot
			raw_path = self.task_dir / 'trajectory' / f'{step}.png'
			raw_path.parent.mkdir(parents=True, exist_ok=True)
			# BrowserRuntime writes the unmodified screenshot before adding element
			# overlays.  Fakes may not, so retain a small compatibility fallback.
			if screenshot and not raw_path.exists():
				raw_path.write_bytes(screenshot)

			if stale_click_recovery_step and not screenshot:
				outcome.status = 'FAIL_ACTIONS'
				outcome.error = 'Stale click recovery requires a fresh screenshot; coordinate recovery was not attempted.'
				break

			# Recovery must reach the model with the fresh screenshot; an automatic
			# verification click would consume its one browser-action opportunity.
			verification_decision = None if stale_click_recovery_step else verification.decide(observation)
			if verification_decision is not None and verification_decision.action is VerificationAction.BLOCKED:
				step_record = {
					'step': step,
					'url': observation.url,
					'thought': 'Verification state machine: the visible challenge did not complete within the bounded wait.',
					'action': {'action': 'verification_blocked'},
					'path_json_action': {'operations': []},
					'outcome': verification_decision.reason,
				}
				outcome.thoughts.append(step_record['thought'])
				outcome.steps.append(step_record)
				outcome.status = 'FAIL_VERIFICATION'
				outcome.error = verification_decision.reason
				break
			if verification_decision is not None and verification_decision.action in {
				VerificationAction.CLICK,
				VerificationAction.CLICK_XY,
				VerificationAction.DRAG,
				VerificationAction.WAIT,
			}:
				payload: dict[str, Any]
				if verification_decision.action is VerificationAction.CLICK:
					payload = {'action': 'click', 'element_id': verification_decision.element_id}
				elif verification_decision.action is VerificationAction.CLICK_XY:
					payload = {'action': 'click_xy', 'x': verification_decision.x, 'y': verification_decision.y}
				elif verification_decision.action is VerificationAction.DRAG:
					payload = {
						'action': 'drag',
						'x': verification_decision.x,
						'y': verification_decision.y,
						'end_x': verification_decision.end_x,
						'end_y': verification_decision.end_y,
					}
					if verification_decision.profile:
						payload['profile'] = verification_decision.profile
				else:
					payload = {'action': 'wait', 'seconds': verification_decision.wait_seconds}
				action_text = json.dumps(
					{**payload, 'source': 'verification_controller'}, ensure_ascii=False, separators=(',', ':')
				)
				thought = f'Verification state machine: {verification_decision.reason}.'
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
					if _is_closed_browser_runtime_result(runtime_result):
						try:
							recovered = await self.runtime.recover_live_task_page()
						except Exception:
							recovered = False
						if recovered:
							if outcome.actions:
								outcome.actions.pop()
							if outcome.thoughts:
								outcome.thoughts.pop()
							consecutive_errors = 0
							last_action_signature = None
							repeated_action_count = 0
							recent_signatures.clear()
							no_change_counts.clear()
							blocked_no_change_signatures.clear()
							blocked_loop_intents.clear()
							step_counter.refund_last()
							last_outcome = (
								'Browser target closed during the verification action; a surviving task page was re-grounded. '
								'Observe it before deciding again.'
							)
							continue
						browser_failure = classify_browser_failure(
							runtime_result.error or runtime_result.summary,
							phase=BrowserFailurePhase.ACTION,
							session_closed=True,
						)
						outcome.status = browser_failure.status
						outcome.error = (
							'Browser session closed with no surviving task pages: '
							f'{runtime_result.error or runtime_result.summary}'
						)
						outcome.browser_failure = browser_failure.payload(recovery_attempted=True)
						break
					last_outcome, action_result_payload, action_failed = _format_runtime_action_result(runtime_result)
				except Exception as exc:
					if is_browser_session_closed_error(exc):
						try:
							recovered = await self.runtime.recover_live_task_page()
						except Exception:
							recovered = False
						if recovered:
							if outcome.actions:
								outcome.actions.pop()
							if outcome.thoughts:
								outcome.thoughts.pop()
							consecutive_errors = 0
							last_action_signature = None
							repeated_action_count = 0
							recent_signatures.clear()
							no_change_counts.clear()
							blocked_no_change_signatures.clear()
							blocked_loop_intents.clear()
							step_counter.refund_last()
							last_outcome = (
								'Browser target closed during the verification action; a surviving task page was re-grounded. '
								'Observe it before deciding again.'
							)
							continue
						browser_failure = classify_browser_failure(
							exc,
							phase=BrowserFailurePhase.ACTION,
							session_closed=True,
						)
						outcome.status = browser_failure.status
						outcome.error = f'Browser session closed with no surviving task pages: {exc}'
						outcome.browser_failure = browser_failure.payload(recovery_attempted=True)
						break
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
			if (
				verification_decision is not None
				and verification_decision.state.value == 'passed'
				and not last_outcome.startswith(_FINISH_FALSE_RETRY_PREFIX)
			):
				last_outcome = 'Visible verification completed; continue in the same browser context.'

			downloads = list(getattr(observation, 'downloads', []) or [])
			data_artifact_notice = self._register_download_artifacts(downloads)
			download_recovery_notice = self._download_recovery_notice(downloads)
			rendered_observation = observation.render_text()
			observation_fingerprint = _observation_hash(rendered_observation)
			answer_priority_mode = exploration_tracker.answer_priority_mode
			exploration_review = exploration_tracker.review_request(current_page_url=observation.url)
			path_tree_ready = False
			root_update_repair = False
			structured_decision_repair_feedback: StructuredDecisionRepairFeedback | None = None
			excluded_model_service_groups: set[str] = set()
			temporarily_hidden_actions: set[str] = {'click'} if stale_click_recovery_step else set()
			action_contract_error_counts: dict[str, int] = {}
			while not path_tree_ready:
				if cancellation_requested():
					break
				try:
					await self._emit('task.phase_changed', payload={'phase': 'model_wait'})
					if cancellation_requested():
						break
					decision, model_usage, prompt_index, model_call_started_at = await self._request_model_decision(
						step=step,
						context=StepContext(
							step_index=step,
							observation=observation,
							history=(
								_stale_click_recovery_history(outcome.steps)
								if stale_click_recovery_step
								else tuple(outcome.steps)
							),
							last_outcome=last_outcome,
							structured_decision_repair_feedback=structured_decision_repair_feedback,
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
							analysis_not_ready_recovery=self._analysis_not_ready_recovery_notice(),
							stale_click_recovery=stale_click_recovery_step,
						),
						screenshot=screenshot,
						raw_path=raw_path if screenshot else None,
					outcome=outcome,
					excluded_service_groups=excluded_model_service_groups,
					root_update_repair=root_update_repair,
					hidden_actions=temporarily_hidden_actions,
				)
					# A valid decision closes the same-step repair context.  Later
					# prompts must not carry an obsolete invalid output forward.
					structured_decision_repair_feedback = None
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
					action_contract_error = _action_name_from_contract_diagnostic(exc.diagnostic)
					if action_contract_error is not None:
						action_contract_error_counts[action_contract_error] = (
							action_contract_error_counts.get(action_contract_error, 0) + 1
					)

					repair_diagnostic = exc.diagnostic
					if (
						action_contract_error is not None
						and action_contract_error_counts[action_contract_error] >= _ACTION_CONTRACT_ERRORS_BEFORE_HIDE
					):
						temporarily_hidden_actions.add(action_contract_error)
						repair_diagnostic = _temporarily_hidden_action_diagnostic(
							exc.diagnostic,
							action_contract_error,
						)
						# Filtering the repeatedly malformed action creates a new choice
						# space. Give that repaired schema a fresh bounded error budget.
						consecutive_model_output_errors = 0
					if exc.source_service_group is not None:
						excluded_model_service_groups.add(exc.source_service_group)
					structured_decision_repair_feedback = StructuredDecisionRepairFeedback(
						diagnostic=repair_diagnostic,
						previous_invalid_decision=exc.previous_invalid_decision,
					)
					last_outcome = (
						'ERROR: The previous model decision output was invalid_decision; no browser action was executed. '
						f'Diagnostic: {repair_diagnostic}. Correct the action and return one valid AgentDecision; '
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
				eligible_data_dirs = self._eligible_data_dirs()
				if decision.action == 'call_data_analysis_assistant' and not eligible_data_dirs:
					self._activate_analysis_not_ready_recovery()
					last_outcome = json.dumps(
						{
							'action': decision.action,
							'status': 'analysis_not_ready',
							'error': 'this task has no eligible ready_data_dir',
							'recovery': _ANALYSIS_NOT_READY_RECOVERY,
						},
						ensure_ascii=False,
						separators=(',', ':'),
					)
					outcome.steps.append(
						{
							'step': step,
							'url': observation.url,
							'thought': decision.thought,
							'action': _decision_action_payload(decision),
							'current_path_id': decision.current_path_id,
							'decision_summary': decision.decision_summary,
							'path_json_action': _path_json_action_artifact_payload(decision),
							'path_json_action_result': {
								'operations': [],
								'blocked_reason': 'analysis action rejected before path updates because no eligible ready_data_dir exists',
							},
							'gate_rejected': True,
							'outcome': last_outcome,
						}
					)
					continue

				if decision.action == 'call_data_analysis_assistant' and decision.data_dir not in eligible_data_dirs:
					diagnostic = 'data_dir must be one of the current eligible ready_data_dir values'
					model_error = f'Model response omitted or violated the data-directory contract: {diagnostic}'
					self._record_model_result(
						step=step,
						prompt_index=prompt_index,
						started_at=model_call_started_at,
						error=model_error,
					)
					consecutive_model_output_errors += 1
					structured_decision_repair_feedback = StructuredDecisionRepairFeedback(diagnostic=diagnostic)
					last_outcome = (
						'ERROR: The previous model decision output was invalid_decision because its data_dir was not eligible; '
						'no browser action or path update was executed. Correct data_dir and return one valid AgentDecision; '
						'this retry does not consume a step.'
					)
					if consecutive_model_output_errors >= self.max_consecutive_model_output_errors:
						outcome.status = 'FAIL_MODEL'
						outcome.error = model_error
						break
					continue

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
						review_requirement_result = exploration_tracker.validate_initial_page_review(
							exploration_review,
							decision.path_json_action,
						)
						path_action_result = review_requirement_result or exploration_tracker.apply_path_json_action_and_activate(
							decision.path_json_action,
							start_url=observation.url,
							current_path_id=decision.current_path_id,
							decision_summary=decision.decision_summary,
							decision_summary_provided='decision_summary' in decision.model_fields_set,
						)
						if path_action_result.answer_priority_mode:
							# The successful mutation itself is the mode switch.  Ignore any
							# remaining same-batch path operations and execute this decision.
							pass
						elif path_action_result.blocked or path_action_result.has_unapplied_operations:
							path_error = _normalized_path_repair_diagnostic(
								path_action_result,
								path_tree=exploration_tracker.payload(),
							)
						else:
							exploration_tracker.accept_review(exploration_review)
					except ExplorationPathError as exc:
						path_action_result = PathJsonActionResult((), str(exc))
						path_error = 'The path-tree state could not be applied. Using the current trusted path tree and the `add`/`update` contract, submit only valid deltas for this decision.'

				if path_error is not None:
					root_update_repair = _requires_root_update_repair_protocol(path_action_result)
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
					structured_decision_repair_feedback = StructuredDecisionRepairFeedback(
						diagnostic=path_error,
					)
					last_outcome = (
						'ERROR: The previous model decision output was invalid_decision because its exploration path state was invalid; '
						'no browser action was executed. '
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
			if cancellation_requested():
				break
			recovery_action_active = stale_click_recovery_step
			if recovery_action_active:
				# The fresh recovery action gets one real browser attempt even if normal
				# loop guards still remember the pre-recovery page generation.
				stale_click_recovery_pending = False
				last_action_signature = None
				repeated_action_count = 0
				recent_signatures.clear()
				no_change_counts.clear()
				blocked_no_change_signatures.clear()
				blocked_loop_intents.clear()
			action_text = _action_string(decision)
			await self._emit_decided_step(step=step, action=decision.action, thought=decision.thought)
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
				'decision_summary': decision.decision_summary,
				'path_json_action': _path_json_action_artifact_payload(decision),
				'path_json_action_result': path_action_result.payload(),
			}

			if decision.action == 'finish':
				if decision.success is True and answer and evidence:
					step_record['outcome'] = 'Task completed with an answer and origin explanation.'
					outcome.steps.append(step_record)
					await self._emit_completed_step(
						step=step, action=decision.action, outcome='ok', thought=decision.thought
					)
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
						decision=decision,
					)
					last_outcome = _finish_false_retry_message(self.task.task)
					continue
				last_outcome = 'ERROR: finish(success=true) requires a non-empty answer and at least one evidence item.'
				step_record['outcome'] = last_outcome
				outcome.steps.append(step_record)
				_record_exploration_decision(
					exploration_tracker,
					decision=decision,
				)
				continue

			if decision.action == 'submit_answer_candidate':
				if not self._completion_enabled:
					outcome.status = 'FAIL_COMPLETION_VERIFICATION'
					outcome.error = 'Completion protocol is unavailable for this decision-only model seam.'
					step_record['outcome'] = outcome.error
					outcome.steps.append(step_record)
					break
				candidate = AnswerCandidate(
					answer=decision.answer or '',
					answer_items=decision.answer_items or [],
					claims=decision.claims or [],
				)
				try:
					gate_result = await self.completion_gate.submit(candidate)
				except CompletionVerificationError as exc:
					outcome.status = 'FAIL_COMPLETION_VERIFICATION'
					outcome.error = str(exc)
					self._sync_completion_outcome(outcome)
					step_record['outcome'] = 'Completion verification could not return a valid result.'
					outcome.steps.append(step_record)
					break
				self._sync_completion_outcome(outcome)
				if gate_result.accepted:
					step_record['outcome'] = 'Completion gate accepted the answer candidate.'
					outcome.steps.append(step_record)
					outcome.status = 'SUCCESS'
					outcome.agent_answer = candidate.answer
					break
				step_record['outcome'] = gate_result.feedback
				outcome.steps.append(step_record)
				last_outcome = gate_result.feedback
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
					decision=decision,
				)
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
							'time is short, finish with the findings already verified in the browser evidence.'
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
					decision=decision,
				)
				# A detected loop is a planning stall, not a browser failure, so it
				# must not consume the consecutive-action-error budget.
				recent_signatures.clear()
				continue

			# Persist the initiated action before awaiting the browser.  A task-wide
			# watchdog may cancel a slow browser operation, but its action, thought,
			# and step still belong in the final timeout artifact.
			await self._emit('task.phase_changed', payload={'phase': 'browser_action'})
			if cancellation_requested():
				break
			step_record['outcome'] = 'Action started; browser result was not recorded yet.'
			outcome.steps.append(step_record)
			action_failed = False
			action_result_payload: dict[str, Any] | None = None
			browser_session_interrupted: str | None = None
			runtime_result: WebRetrieverActionResult | str | None = None
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
					ready_data_dir = self._ready_data_dir_key(decision.data_dir)
					filter_mismatch = self._data_filter_mismatch(decision.data_dir, decision.analysis_query)
					if ready_data_dir is None:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'invalid_data_dir',
								'error': 'data_dir must be the exact ready directory returned by a data artifact in this task run',
							},
							separators=(',', ':'),
						)
						action_failed = True
					elif ready_data_dir in self._unavailable_analysis_data_dirs:
						last_outcome = json.dumps(
							{
								'action': decision.action,
								'status': 'analysis_unavailable',
								'error': 'this data_dir was previously rejected as statically unavailable for analysis',
								'recovery': (
									'do not retry this data_dir with call_data_analysis_assistant; use page content, '
									'an official export, or a different first-party source'
								),
							},
							separators=(',', ':'),
						)
						action_failed = False
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
						if _is_non_retryable_analysis_status(analysis_payload):
							self._unavailable_analysis_data_dirs.add(ready_data_dir)
							if not self._eligible_data_dirs():
								self._activate_analysis_not_ready_recovery()
							analysis_payload = dict(analysis_payload)
							analysis_payload['recovery'] = (
								'do not retry this data_dir with call_data_analysis_assistant; use page content, '
								'an official export, or a different first-party source'
							)
							last_outcome = json.dumps(analysis_payload, ensure_ascii=False, separators=(',', ':'))
							action_failed = False
						else:
							action_failed = not isinstance(analysis_payload, dict) or analysis_payload.get('status') != 'ok'
				else:
					runtime_result = await self.runtime.execute(decision)
					if _is_closed_browser_runtime_result(runtime_result):
						try:
							recovered = await self.runtime.recover_live_task_page()
						except Exception:
							recovered = False
						if recovered:
							if outcome.actions:
								outcome.actions.pop()
							if outcome.thoughts:
								outcome.thoughts.pop()
							if outcome.steps and outcome.steps[-1] is step_record:
								outcome.steps.pop()
							if recent_signatures and recent_signatures[-1] == (action_intent, observation_fingerprint):
								recent_signatures.pop()
							last_action_signature = None
							repeated_action_count = 0
							consecutive_errors = 0
							recent_signatures.clear()
							no_change_counts.clear()
							blocked_no_change_signatures.clear()
							blocked_loop_intents.clear()
							step_counter.refund_last()
							last_outcome = (
								'Browser target closed during the previous action; a surviving task page was re-grounded. '
								'Observe it before deciding again.'
							)
							continue
						browser_session_interrupted = runtime_result.error or runtime_result.summary
					last_outcome, action_result_payload, action_failed = _format_runtime_action_result(runtime_result)
			except TimeoutError:
				last_outcome = json.dumps(
					{'action': decision.action, 'status': 'timeout', 'error': 'action exceeded its shared task budget'},
					separators=(',', ':'),
				)
				action_failed = True
			except Exception as exc:
				if is_browser_session_closed_error(exc):
					try:
						recovered = await self.runtime.recover_live_task_page()
					except Exception:
						recovered = False
					if recovered:
						if outcome.actions:
							outcome.actions.pop()
						if outcome.thoughts:
							outcome.thoughts.pop()
						if outcome.steps and outcome.steps[-1] is step_record:
							outcome.steps.pop()
						if recent_signatures and recent_signatures[-1] == (action_intent, observation_fingerprint):
							recent_signatures.pop()
						last_action_signature = None
						repeated_action_count = 0
						consecutive_errors = 0
						recent_signatures.clear()
						no_change_counts.clear()
						blocked_no_change_signatures.clear()
						blocked_loop_intents.clear()
						step_counter.refund_last()
						last_outcome = (
							'Browser target closed during the previous action; a surviving task page was re-grounded. '
							'Observe it before deciding again.'
						)
						continue
					browser_session_interrupted = str(exc)
				last_outcome = f'ERROR: {type(exc).__name__}: {exc}'
				action_failed = True

			path_action_feedback = path_action_result.feedback()
			if path_action_feedback:
				last_outcome = _append_path_action_feedback(last_outcome, path_action_feedback)

			if self._completion_enabled and not action_failed and browser_session_interrupted is None:
				evidence_output = (
					runtime_result.extracted_content
					if isinstance(runtime_result, WebRetrieverActionResult) and runtime_result.extracted_content is not None
					else last_outcome
				)
				self.completion_gate.register_action(
					action=decision.action,
					step=step + 1,
					source_url=observation.url,
					output=evidence_output,
					parameters=decision.action_payload(),
					screenshot=screenshot,
				)
				self._sync_completion_outcome(outcome)

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
			telemetry_outcome = (
				action_result_payload.get('status')
				if isinstance(action_result_payload, Mapping) and isinstance(action_result_payload.get('status'), str)
				else ('error' if action_failed else 'ok')
			)
			await self._emit_completed_step(
				step=step, action=decision.action, outcome=telemetry_outcome, thought=decision.thought
			)
			_record_exploration_decision(
				exploration_tracker,
				decision=decision,
			)
			if browser_session_interrupted is not None:
				browser_failure = classify_browser_failure(
					browser_session_interrupted,
					phase=BrowserFailurePhase.ACTION,
					session_closed=True,
				)
				outcome.status = browser_failure.status
				outcome.error = (
					'Browser session closed with no surviving task pages: '
					f'{browser_session_interrupted}'
				)
				outcome.browser_failure = browser_failure.payload(recovery_attempted=True)
				break
			if action_failed:
				consecutive_errors += 1
			else:
				consecutive_errors = 0

			if recovery_action_active and action_failed:
				outcome.status = 'FAIL_ACTIONS'
				outcome.error = f'Stale click recovery action failed; last error: {last_outcome}'
				break

			if consecutive_errors >= self.max_consecutive_action_errors:
				if _is_stale_click_recovery_candidate(
					action=decision.action,
					action_result_payload=action_result_payload,
				):
					stale_click_recovery_pending = True
					consecutive_errors = 0
					last_outcome = _STALE_CLICK_RECOVERY_LAST_OUTCOME
					continue
				outcome.status = 'FAIL_ACTIONS'
				outcome.error = f'{consecutive_errors} consecutive browser actions failed; last error: {last_outcome}'
				break
		else:
			outcome.status = 'FAIL_MAX_STEPS'
			outcome.error = f'Reached the competition limit of {self.max_steps} steps without a final answer'

		outcome.duration_seconds = round(time.monotonic() - started_at, 3)
		outcome.verification = verification.summary()
		self._update_timing_summary(outcome)
		return outcome
