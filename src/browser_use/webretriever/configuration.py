"""TOML configuration-file support for the WebRetriever runner.

The command-line interface remains useful for one-off overrides, while this
module keeps the durable run settings in one documented, validated file.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_SECTION = 'webretriever'


class ConfigurationError(ValueError):
	"""Raised when a WebRetriever TOML configuration file is malformed."""


@dataclass(frozen=True, slots=True)
class FileConfiguration:
	"""The validated settings loaded from one ``[webretriever]`` TOML section."""

	path: Path
	values: Mapping[str, Any]

	def get(self, key: str, default: Any = None) -> Any:
		return self.values.get(key, default)

	def has(self, key: str) -> bool:
		return key in self.values


_STRING_FIELDS = frozenset(
	{
		'input_path',
		'output_dir',
		'model',
		'sec_user_agent',
		'thought_language',
	}
)
_BOOLEAN_FIELDS = frozenset(
	{
		'structured_prompt_log',
		'local_browser',
		'headless',
		'rerun_failed',
		'validate_only',
	}
)
_INTEGER_FIELDS = frozenset({'max_steps', 'max_concurrency', 'limit'})
_NUMBER_FIELDS = frozenset({'model_timeout_seconds', 'task_timeout_seconds'})
_STRING_LIST_FIELDS = frozenset({'cdp_urls'})
_INTEGER_LIST_FIELDS = frozenset({'vlm_ports'})
_CHOICE_FIELDS: dict[str, frozenset[str]] = {
	'api_mode': frozenset({'auto', 'responses', 'chat-completions'}),
	'reasoning_effort': frozenset({'low', 'medium', 'high'}),
}
_CONFIG_FIELDS = (
	_STRING_FIELDS
	| _BOOLEAN_FIELDS
	| _INTEGER_FIELDS
	| _NUMBER_FIELDS
	| _STRING_LIST_FIELDS
	| _INTEGER_LIST_FIELDS
	| frozenset(_CHOICE_FIELDS)
	| frozenset({'task_indices', 'model_services'})
)


def _is_integer(value: object) -> bool:
	return type(value) is int


def _validate_value(path: Path, key: str, value: Any) -> None:
	prefix = f'{path}: [{CONFIG_SECTION}].{key}'
	if key in _STRING_FIELDS:
		if not isinstance(value, str):
			raise ConfigurationError(f'{prefix} must be a string')
		return
	if key in _BOOLEAN_FIELDS:
		if type(value) is not bool:
			raise ConfigurationError(f'{prefix} must be true or false')
		return
	if key in _INTEGER_FIELDS:
		if not _is_integer(value):
			raise ConfigurationError(f'{prefix} must be an integer')
		return
	if key in _NUMBER_FIELDS:
		if type(value) not in {int, float}:
			raise ConfigurationError(f'{prefix} must be a number')
		return
	if key in _STRING_LIST_FIELDS:
		if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
			raise ConfigurationError(f'{prefix} must be a list of strings')
		return
	if key in _INTEGER_LIST_FIELDS:
		if not isinstance(value, list) or not all(_is_integer(item) for item in value):
			raise ConfigurationError(f'{prefix} must be a list of integers')
		return
	if key == 'task_indices':
		if isinstance(value, str):
			return
		if isinstance(value, list) and all(isinstance(item, (str, int)) and not isinstance(item, bool) for item in value):
			return
		raise ConfigurationError(f'{prefix} must be a task-index string or list of strings/integers')
	if key == 'model_services':
		if not isinstance(value, list) or not value:
			raise ConfigurationError(f'{prefix} must be a non-empty list of tables')
		for index, service in enumerate(value):
			service_prefix = f'{prefix}[{index}]'
			if not isinstance(service, dict):
				raise ConfigurationError(f'{service_prefix} must be a table')
			if set(service) != {'name', 'api_base', 'api_key'}:
				missing = sorted({'name', 'api_base', 'api_key'} - set(service))
				unknown = sorted(set(service) - {'name', 'api_base', 'api_key'})
				details = []
				if missing:
					details.append(f'missing {", ".join(missing)}')
				if unknown:
					details.append(f'unsupported {", ".join(unknown)}')
				raise ConfigurationError(f'{service_prefix} must contain name, api_base, and api_key ({"; ".join(details)})')
			for field_name in ('name', 'api_base', 'api_key'):
				if not isinstance(service[field_name], str):
					raise ConfigurationError(f'{service_prefix}.{field_name} must be a string')
		return
	if key in _CHOICE_FIELDS:
		if not isinstance(value, str) or value not in _CHOICE_FIELDS[key]:
			choices = ', '.join(sorted(_CHOICE_FIELDS[key]))
			raise ConfigurationError(f'{prefix} must be one of: {choices}')
		return
	raise AssertionError(f'unhandled configuration key: {key}')


def load_file_configuration(path: Path) -> FileConfiguration:
	"""Load a strictly validated WebRetriever TOML configuration file."""

	try:
		with path.open('rb') as config_file:
			payload = tomllib.load(config_file)
	except FileNotFoundError as exc:
		raise ConfigurationError(f'configuration file not found: {path}') from exc
	except OSError as exc:
		raise ConfigurationError(f'could not read configuration file {path}: {exc}') from exc
	except tomllib.TOMLDecodeError as exc:
		raise ConfigurationError(f'invalid TOML in {path}: {exc}') from exc

	if set(payload) != {CONFIG_SECTION}:
		raise ConfigurationError(f'{path} must contain exactly one [{CONFIG_SECTION}] section')
	values = payload[CONFIG_SECTION]
	if not isinstance(values, dict):
		raise ConfigurationError(f'{path}: [{CONFIG_SECTION}] must be a TOML table')

	unknown = sorted(set(values) - _CONFIG_FIELDS)
	if unknown:
		raise ConfigurationError(
			f'{path}: unsupported [{CONFIG_SECTION}] setting(s): {", ".join(unknown)}'
		)
	for key, value in values.items():
		_validate_value(path, key, value)
	return FileConfiguration(path=path, values=values)


__all__ = ['ConfigurationError', 'FileConfiguration', 'load_file_configuration']
