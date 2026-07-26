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
from pathlib import Path
from typing import Any, Literal, TypeAlias, get_args
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
	cursor: str | None = None
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
				return value
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
		return data

	@field_validator('text', 'url', 'key', 'answer', 'cursor', 'analysis_query', 'data_dir')
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
		required_by_action: dict[ActionName, frozenset[str]] = {
			'click': frozenset({'element_id'}),
			'double_click': frozenset({'element_id'}),
			'type': frozenset({'element_id', 'text'}),
			'select': frozenset({'element_id', 'text'}),
			'press': frozenset({'key'}),
			'scroll': frozenset({'direction', 'pages'}),
			'hover': frozenset({'element_id'}),
			'click_xy': frozenset({'x', 'y'}),
			'hover_xy': frozenset({'x', 'y'}),
			'drag': frozenset({'x', 'y', 'end_x', 'end_y'}),
			'back': frozenset(),
			'navigate': frozenset({'url'}),
			'wait': frozenset({'seconds'}),
			'switch_tab': frozenset({'tab_index'}),
			'close_tab': frozenset({'tab_index'}),
			'read_element': frozenset({'element_id'}),
			'find_text': frozenset({'text'}),
			'inspect_network': frozenset(),
			'find_chart_data_requests': frozenset(),
			'call_data_analysis_assistant': frozenset({'analysis_query', 'data_dir'}),
			'calculate': frozenset({'operation', 'text'}),
			'finish': frozenset({'success'}),
		}
		optional_by_action: dict[ActionName, frozenset[str]] = {
			'press': frozenset({'element_id'}),
			'scroll': frozenset({'element_id'}),
			'inspect_network': frozenset({'text', 'request_id', 'cursor'}),
			'find_chart_data_requests': frozenset({'cursor'}),
			'finish': frozenset({'answer', 'evidence'}),
		}
		parameter_names = {
			'element_id',
			'text',
			'url',
			'key',
			'x',
			'y',
			'end_x',
			'end_y',
			'direction',
			'pages',
			'seconds',
			'tab_index',
			'request_id',
			'answer',
			'evidence',
			'success',
			'operation',
			'cursor',
			'analysis_query',
			'data_dir',
		}
		required = required_by_action[self.action]
		missing = sorted(name for name in required if getattr(self, name) is None)
		if missing:
			raise ValueError(f'{self.action} requires: {", ".join(missing)}')

		allowed = required | optional_by_action.get(self.action, frozenset())
		unexpected = sorted(name for name in parameter_names - allowed if getattr(self, name) is not None)
		if unexpected:
			raise ValueError(f'{self.action} does not accept: {", ".join(unexpected)}')

		if self.action == 'navigate' and self.url is not None:
			_validate_web_url(self.url, field_name='url')
		if self.action == 'inspect_network':
			if self.text is not None and self.request_id is not None:
				raise ValueError('inspect_network text and request_id are mutually exclusive')
			if self.cursor is not None and self.request_id is None:
				raise ValueError('inspect_network cursor requires request_id')
		if self.action == 'finish' and self.success:
			if self.answer is None:
				raise ValueError('a successful finish requires answer')
			if self.evidence is None:
				raise ValueError('a successful finish requires evidence')
		return self

	def action_payload(self) -> dict[str, Any]:
		"""Serialize the decision without null placeholders."""

		return self.model_dump(exclude_none=True)


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
