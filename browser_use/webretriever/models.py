"""Validated data models for the WebRetriever Protocol III adapter.

The public challenge dataset contains a reference ``answer``.  That value is
useful for offline evaluation, but it must never be exposed to the agent.  This
module therefore accepts known ground-truth keys only long enough to discard
them; they are not model fields and cannot appear in any model serialization.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, get_args
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from browser_use.webretriever.strategy import CHECKPOINT_DECISION_FIELD_LIMITS, CHECKPOINT_DECISION_FIELDS

ActionName: TypeAlias = Literal[
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
]
ScrollDirection: TypeAlias = Literal['up', 'down', 'left', 'right']
CalculationOperation: TypeAlias = Literal[
	'argmax',
	'argmin',
	'argmax_difference',
	'argmin_difference',
	'argmax_growth',
	'argmin_growth',
]
Evidence: TypeAlias = list[str]


@dataclass(frozen=True, slots=True)
class ActionParameterContract:
	"""Required and optional flat fields for one structured browser action."""

	required: frozenset[str] = frozenset()
	optional: frozenset[str] = frozenset()
	description: str = ''


ACTION_PARAMETER_CONTRACTS: dict[ActionName, ActionParameterContract] = {
	'click': ActionParameterContract(frozenset({'element_id'}), description='click a current semantic element'),
	'double_click': ActionParameterContract(frozenset({'element_id'}), description='double-click a current element'),
	'type': ActionParameterContract(frozenset({'element_id', 'text'}), description='replace an input value'),
	'select': ActionParameterContract(frozenset({'element_id', 'text'}), description='choose a visible option label/value'),
	'press': ActionParameterContract(frozenset({'key'}), frozenset({'element_id'}), 'press one key'),
	'scroll': ActionParameterContract(
		frozenset({'direction', 'pages'}), frozenset({'element_id'}), 'scroll the page or a current region'
	),
	'hover': ActionParameterContract(frozenset({'element_id'}), description='hover a semantic element'),
	'click_xy': ActionParameterContract(frozenset({'x', 'y'}), description='click screenshot coordinates'),
	'hover_xy': ActionParameterContract(frozenset({'x', 'y'}), description='hover screenshot coordinates'),
	'drag': ActionParameterContract(frozenset({'x', 'y', 'end_x', 'end_y'}), description='drag between coordinates'),
	'back': ActionParameterContract(description='navigate back'),
	'navigate': ActionParameterContract(frozenset({'url'}), description='open an observed or first-party absolute URL'),
	'wait': ActionParameterContract(frozenset({'seconds'}), description='wait at most 30 seconds'),
	'switch_tab': ActionParameterContract(frozenset({'tab_index'}), description='activate a current tab'),
	'close_tab': ActionParameterContract(frozenset({'tab_index'}), description='close a current tab'),
	'read_element': ActionParameterContract(frozenset({'element_id'}), description='read a current element fully'),
	'find_text': ActionParameterContract(frozenset({'text'}), description='find text on the current page/document'),
	'inspect_network': ActionParameterContract(
		optional=frozenset({'text', 'request_id', 'network_cursor'}),
		description='search captured bodies, optionally scoped to request_id, or continue one captured response',
	),
	'find_chart_data_requests': ActionParameterContract(
		optional=frozenset({'chart_cursor'}), description='normalize current chart traffic or continue its saved packet'
	),
	'call_data_analysis_assistant': ActionParameterContract(
		frozenset({'analysis_query', 'data_dir'}), description='analyze a validated task-local data artifact'
	),
	'calculate': ActionParameterContract(
		frozenset({'operation', 'text'}), description='calculate over browser-observed JSON numbers'
	),
	'finish': ActionParameterContract(
		frozenset({'success'}), frozenset({'answer', 'evidence'}), 'finish with grounded answer/evidence or explicit failure'
	),
}


def render_action_parameter_contracts() -> str:
	"""Render the validator's field map as concise model-facing instructions."""

	lines: list[str] = []
	for action in get_args(ActionName):
		contract = ACTION_PARAMETER_CONTRACTS[action]
		field_parts: list[str] = []
		if contract.required:
			field_parts.append('required: ' + ', '.join(sorted(contract.required)))
		if contract.optional:
			field_parts.append('optional: ' + ', '.join(sorted(contract.optional)))
		fields = '; '.join(field_parts) if field_parts else 'no parameters'
		lines.append(f'- {action} ({fields}): {contract.description}.')
	return '\n'.join(lines)


_TASK_ALIASES: dict[str, tuple[str, ...]] = {
	'task_idx': ('task_index', 'idx', 'index'),
	'task_id': ('id',),
	'website': ('website_url', 'start_url', 'url'),
	'task': ('instruction', 'query', 'question'),
}
_GROUND_TRUTH_KEYS = frozenset({'answer', 'ground_truth', 'ground_truth_answer', 'gold_answer', 'reference_answer'})
_SAFE_TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def _validate_web_url(value: str, *, field_name: str) -> str:
	"""Validate an HTTP(S) URL without normalizing the caller's representation."""

	if any(character.isspace() or ord(character) < 32 for character in value):
		raise ValueError(f'{field_name} must not contain whitespace or control characters')
	parts = urlsplit(value)
	if parts.scheme.lower() not in {'http', 'https'} or not parts.netloc or parts.hostname is None:
		raise ValueError(f'{field_name} must be an absolute HTTP(S) URL')
	if parts.username is not None or parts.password is not None:
		raise ValueError(f'{field_name} must not contain credentials')
	try:
		_ = parts.port
	except ValueError as exc:
		raise ValueError(f'{field_name} contains an invalid port') from exc
	return value


class CompetitionTask(BaseModel):
	"""One validated challenge task, with ground truth intentionally omitted.

	The loader accepts the canonical challenge names as well as common template
	aliases.  Serialization always uses the four canonical, agent-safe fields.
	"""

	# Official templates may add evaluator-owned metadata.  Ignore it and expose
	# only the four canonical fields through prompt_payload().
	model_config = ConfigDict(extra='ignore', frozen=True, strict=True, str_strip_whitespace=True)

	task_idx: int = Field(
		ge=0,
		validation_alias=AliasChoices('task_idx', 'task_index', 'idx', 'index'),
	)
	task_id: str = Field(
		min_length=1,
		max_length=128,
		validation_alias=AliasChoices('task_id', 'id'),
	)
	website: str = Field(
		min_length=1,
		validation_alias=AliasChoices('website', 'website_url', 'start_url', 'url'),
	)
	task: str = Field(
		min_length=1,
		validation_alias=AliasChoices('task', 'instruction', 'query', 'question'),
	)

	@model_validator(mode='before')
	@classmethod
	def _normalize_aliases_and_drop_ground_truth(cls, value: Any) -> Any:
		if not isinstance(value, Mapping):
			return value

		data = dict(value)
		for secret_key in _GROUND_TRUTH_KEYS:
			data.pop(secret_key, None)

		for canonical_name, aliases in _TASK_ALIASES.items():
			present_names = [name for name in (canonical_name, *aliases) if name in data]
			if not present_names:
				continue
			first_name = present_names[0]
			first_value = data[first_name]
			conflicts = [name for name in present_names[1:] if data[name] != first_value]
			if conflicts:
				raise ValueError(f'conflicting aliases for {canonical_name}: {", ".join(present_names)}')
			data[canonical_name] = first_value
			for alias in aliases:
				data.pop(alias, None)
		return data

	@field_validator('task_id')
	@classmethod
	def _task_id_is_path_safe(cls, value: str) -> str:
		if not _SAFE_TASK_ID.fullmatch(value):
			raise ValueError(
				'task_id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens and must begin with a letter or digit'
			)
		if value in {'.', '..'} or '..' in value:
			raise ValueError('task_id must not contain a parent-directory segment')
		return value

	@field_validator('website')
	@classmethod
	def _website_is_valid(cls, value: str) -> str:
		return _validate_web_url(value, field_name='website')

	def prompt_payload(self) -> dict[str, int | str]:
		"""Return the only task representation that may be sent to a model."""

		return {
			'task_idx': self.task_idx,
			'task_id': self.task_id,
			'website': self.website,
			'task': self.task,
		}

	@property
	def directory_name(self) -> str:
		"""Competition-mandated, path-safe artifact directory name."""

		return f'{self.task_idx}_{self.task_id}'


class AgentDecision(BaseModel):
	"""A flat, structured next-action response produced by the agent model.

	Only parameters relevant to the selected action may be populated.  Keeping
	the schema flat makes it reliable with OpenAI-compatible structured-output
	APIs while the post-validator still provides discriminated-action semantics.
	"""

	model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)

	action: ActionName
	thought: str = ''
	memory: str = ''
	# Strict structured-output providers require every flat property on every
	# turn.  These values are therefore null outside a prompted checkpoint and
	# all non-empty when a checkpoint is due; they are never action parameters.
	checkpoint_strategy_catalog: str | None = Field(
		default=None,
		max_length=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_strategy_catalog'],
		description=(
			'Checkpoint only: all legal, materially distinct viable strategy classes. '
			'When populated, use a multi-line Markdown list value: one class per line, each beginning "- "; '
			'do not use inline numbering or combine classes on one line.'
		),
	)
	checkpoint_active_strategy: str | None = Field(
		default=None,
		max_length=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_active_strategy'],
		description=(
			'Checkpoint only: the strategy currently being attempted. When populated, use a Markdown list with exactly one '
			'item beginning "- ".'
		),
	)
	checkpoint_confirmed_infeasible: str | None = Field(
		default=None,
		max_length=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_confirmed_infeasible'],
		description=(
			'Checkpoint only: strategies ruled out by browser-grounded evidence. When populated, use a Markdown list: '
			'one item per line, each beginning "- ".'
		),
	)
	checkpoint_next_strategies: str | None = Field(
		default=None,
		max_length=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_next_strategies'],
		description=(
			'Checkpoint only: remaining legal strategies worth attempting next. When populated, use a Markdown list: '
			'one item per line, each beginning "- ".'
		),
	)
	element_id: int | None = Field(default=None, ge=0)
	text: str | None = None
	url: str | None = None
	key: str | None = None
	x: int | None = Field(default=None, ge=0)
	y: int | None = Field(default=None, ge=0)
	end_x: int | None = Field(default=None, ge=0)
	end_y: int | None = Field(default=None, ge=0)
	direction: ScrollDirection | None = None
	pages: int | None = Field(default=None, ge=1)
	seconds: float | None = Field(default=None, gt=0, le=30)
	tab_index: int | None = Field(default=None, ge=0)
	request_id: int | None = Field(default=None, ge=0)
	answer: str | None = None
	evidence: Evidence | None = None
	success: bool | None = None
	operation: CalculationOperation | None = None
	network_cursor: str | None = None
	chart_cursor: str | None = None
	analysis_query: str | None = None
	data_dir: str | None = None

	@model_validator(mode='before')
	@classmethod
	def _flatten_nested_action(cls, value: Any) -> Any:
		"""Accept occasional action envelopes emitted by compatible gateways.

		The advertised schema stays flat, but some Responses API proxies return
		``{"action": {"action": "click", ...}, ...}`` or
		``{"click": {"element_id": 1}, ...}``. Normalize those unambiguous wire
		variants before strict validation. Conflicting values remain invalid.
		"""
		if not isinstance(value, Mapping):
			return value

		data = dict(value)
		nested_value = data.get('action')
		if isinstance(nested_value, Mapping):
			nested = dict(nested_value)
			data['action'] = nested.pop('action', None)
		else:
			if 'action' in data:
				nested = {}
			else:
				action_names = set(get_args(ActionName))
				candidates = [name for name in data if name in action_names and isinstance(data[name], Mapping)]
				if len(candidates) != 1:
					return value
				action_name = candidates[0]
				nested = dict(data.pop(action_name))
				data['action'] = action_name

		for field_name, field_value in nested.items():
			if field_name in data and data[field_name] != field_value:
				raise ValueError(f'conflicting nested action field: {field_name}')
			data[field_name] = field_value

		# ``cursor`` was the historical shared wire field.  Normalize it before
		# strict validation so it remains accepted without appearing in the
		# recommended JSON schema or permitting cross-tool cursor reuse.
		if 'cursor' in data:
			action = data.get('action')
			typed_field = {
				'inspect_network': 'network_cursor',
				'find_chart_data_requests': 'chart_cursor',
			}.get(action if isinstance(action, str) else '')
			if typed_field is None:
				return data
			legacy_cursor = data.pop('cursor')
			if typed_field in data and data[typed_field] != legacy_cursor:
				raise ValueError(f'conflicting cursor fields for {action}')
			data[typed_field] = legacy_cursor
		return data

	@field_validator(
		'text',
		'url',
		'key',
		'answer',
		'network_cursor',
		'chart_cursor',
		'analysis_query',
		'data_dir',
		'checkpoint_strategy_catalog',
		'checkpoint_active_strategy',
		'checkpoint_confirmed_infeasible',
		'checkpoint_next_strategies',
	)
	@classmethod
	def _non_empty_optional_string(cls, value: str | None) -> str | None:
		if value is not None and not value:
			raise ValueError('value must not be empty')
		return value

	@field_validator('evidence')
	@classmethod
	def _evidence_is_non_empty(cls, value: Evidence | None) -> Evidence | None:
		if value is None:
			return None
		if not value or any(not item for item in value):
			raise ValueError('evidence entries must not be empty')
		return value

	@model_validator(mode='after')
	def _validate_action_parameters(self) -> AgentDecision:
		parameter_names = frozenset(
			field_name
			for contract in ACTION_PARAMETER_CONTRACTS.values()
			for field_name in contract.required | contract.optional
		)
		contract = ACTION_PARAMETER_CONTRACTS[self.action]
		required = contract.required
		missing = sorted(name for name in required if getattr(self, name) is None)
		if missing:
			raise ValueError(f'{self.action} requires: {", ".join(missing)}')

		allowed = required | contract.optional
		unexpected = sorted(name for name in parameter_names - allowed if getattr(self, name) is not None)
		if unexpected:
			raise ValueError(f'{self.action} does not accept: {", ".join(unexpected)}')

		if self.action == 'navigate' and self.url is not None:
			_validate_web_url(self.url, field_name='url')
		if self.action == 'inspect_network':
			if self.network_cursor is not None and self.request_id is None:
				raise ValueError('inspect_network network_cursor requires request_id')
			if self.text is not None and self.network_cursor is not None:
				raise ValueError('inspect_network network_cursor cannot be combined with text')
		if self.action == 'finish' and self.success:
			if self.answer is None:
				raise ValueError('a successful finish requires answer')
			if self.evidence is None:
				raise ValueError('a successful finish requires evidence')
		return self

	def action_payload(self) -> dict[str, Any]:
		"""Serialize browser-action fields without null placeholders or planning metadata."""

		payload = self.model_dump(exclude_none=True)
		for field_name in CHECKPOINT_DECISION_FIELDS:
			payload.pop(field_name, None)
		return payload


def _decode_task_document(path: Path, source: str) -> list[Any]:
	"""Decode a JSON array/wrapper or fall back to line-delimited JSON."""

	try:
		document = json.loads(source)
	except json.JSONDecodeError:
		items: list[Any] = []
		for line_number, raw_line in enumerate(source.splitlines(), start=1):
			line = raw_line.strip()
			if not line:
				continue
			try:
				items.append(json.loads(line))
			except json.JSONDecodeError as line_error:
				raise ValueError(f'{path}: invalid JSONL on line {line_number}: {line_error.msg}') from line_error
		return items

	if isinstance(document, list):
		return document
	if isinstance(document, dict) and 'tasks' in document:
		items = document['tasks']
		if not isinstance(items, list):
			raise ValueError(f'{path}: top-level "tasks" must be an array')
		return items
	if isinstance(document, dict):
		# A one-record JSONL file is also a valid JSON document.  Treat it as
		# JSONL rather than rejecting it merely because it has a single line.
		return [document]
	if document is None and not source.strip():
		return []
	raise ValueError(f'{path}: expected a JSON array, JSONL records, or an object with a top-level "tasks" array')


def load_tasks(path: Path | str) -> list[CompetitionTask]:
	"""Load tasks from a JSON array, JSONL file, or ``{"tasks": [...]}``.

	Both ``task_idx`` and ``task_id`` must be unique.  Validation errors identify
	the input position while preserving the underlying Pydantic exception as the
	cause.
	"""

	task_path = Path(path)
	try:
		source = task_path.read_text(encoding='utf-8-sig')
	except OSError as exc:
		raise ValueError(f'could not read task file {task_path}: {exc}') from exc

	if not source.strip():
		return []
	items = _decode_task_document(task_path, source)
	tasks: list[CompetitionTask] = []
	seen_indices: dict[int, int] = {}
	seen_ids: dict[str, int] = {}
	for position, item in enumerate(items):
		try:
			task = CompetitionTask.model_validate(item)
		except ValidationError as exc:
			raise ValueError(f'{task_path}: invalid task at position {position}') from exc

		if task.task_idx in seen_indices:
			raise ValueError(
				f'{task_path}: duplicate task_idx {task.task_idx} at positions {seen_indices[task.task_idx]} and {position}'
			)
		if task.task_id in seen_ids:
			raise ValueError(
				f'{task_path}: duplicate task_id {task.task_id!r} at positions {seen_ids[task.task_id]} and {position}'
			)
		seen_indices[task.task_idx] = position
		seen_ids[task.task_id] = position
		tasks.append(task)
	return tasks


__all__ = [
	'ActionName',
	'AgentDecision',
	'CalculationOperation',
	'CompetitionTask',
	'Evidence',
	'ScrollDirection',
	'load_tasks',
]
