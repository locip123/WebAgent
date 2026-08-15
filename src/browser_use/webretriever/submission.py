"""Fixed-interface adapter for the WebRetriever Challenge submission template.

The local development CLI intentionally has many debugging switches.  The
competition invokes a much smaller, positional interface instead.  This module
keeps that boundary explicit: it accepts only the evaluator's task file,
output directory, and supplied CDP endpoints; it always uses stock Playwright
and the published limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from browser_use.webretriever.connection import BrowserDriver
from browser_use.webretriever.runner import MAX_CONCURRENCY, RunnerConfig, run

_CDP_ACCESS_TOKEN_KEY = 'access_token'
_DEFAULT_TASK_TIMEOUT_SECONDS = 600.0


def _template_root() -> Path:
	"""Return the repository root when this module is vendored below ``src``."""

	return Path(__file__).resolve().parents[3]


def _load_model_configuration(path: Path) -> dict[str, str]:
	"""Read the template's simple model configuration without leaking its key."""

	try:
		with path.open(encoding='utf-8') as config_file:
			payload = json.load(config_file)
	except FileNotFoundError as exc:
		raise ValueError(f'model configuration file not found: {path}') from exc
	except json.JSONDecodeError as exc:
		raise ValueError(f'model configuration is not valid JSON: {path}') from exc
	if not isinstance(payload, Mapping):
		raise ValueError('model configuration must be a JSON object')

	values: dict[str, str] = {}
	for source_key, target_key in (('api_base', 'api_base'), ('api_key', 'api_key'), ('api_model', 'model')):
		value = payload.get(source_key)
		if not isinstance(value, str) or not value.strip():
			raise ValueError(f'config.json field {source_key!r} must be a non-empty string')
		values[target_key] = value.strip()
	for source_key, allowed, default in (
		('api_mode', {'auto', 'responses', 'chat-completions'}, 'responses'),
		('reasoning_effort', {'low', 'medium', 'high'}, 'low'),
	):
		value = payload.get(source_key, default)
		if not isinstance(value, str) or value not in allowed:
			choices = ', '.join(sorted(allowed))
			raise ValueError(f'config.json field {source_key!r} must be one of: {choices}')
		values[source_key] = value
	return values


def _header_from_template(cdp_url: str) -> dict[str, str]:
	"""Reuse the template helper for one evaluator endpoint's cached token.

	The template stores its header cache globally because its reference runner
	uses one process per worker.  This adapter immediately copies the returned
	header before yielding to another async worker, keeping eight distinct CDP
	endpoints isolated in the single-process runtime.
	"""

	try:
		from agent.web_controller import connect_existing_sandbox, get_cdp_headers
	except ImportError as exc:  # pragma: no cover - only exercised outside the template
		raise RuntimeError('the submission adapter requires src/agent/web_controller.py') from exc

	try:
		query = parse_qsl(urlsplit(cdp_url).query, keep_blank_values=True)
	except (TypeError, ValueError):
		query = []
	for key, value in query:
		if key == _CDP_ACCESS_TOKEN_KEY and value:
			connect_existing_sandbox(cdp_url, value)
			break
	headers = get_cdp_headers()
	if not isinstance(headers, Mapping):
		raise ValueError('template CDP header helper returned an invalid value')
	return {str(key): str(value) for key, value in headers.items()}


def build_submission_config(
	*,
	task_file: Path,
	output_dir: Path,
	cdp_urls: Sequence[str],
	config_path: Path | None = None,
	header_provider: Callable[[str], Mapping[str, str]] = _header_from_template,
) -> RunnerConfig:
	"""Build the only runner configuration valid for a formal submission."""

	if not cdp_urls:
		raise ValueError('the evaluator must provide at least one CDP URL')
	if len(cdp_urls) > MAX_CONCURRENCY:
		raise ValueError(f'the competition permits at most {MAX_CONCURRENCY} CDP URLs')
	model = _load_model_configuration(config_path or _template_root() / 'config.json')
	return RunnerConfig(
		input_path=task_file,
		output_dir=output_dir,
		model=model['model'],
		api_key=model['api_key'],
		api_base=model['api_base'],
		cdp_urls=list(cdp_urls),
		max_steps=100,
		model_timeout_seconds=180.0,
		task_timeout_seconds=_DEFAULT_TASK_TIMEOUT_SECONDS,
		max_concurrency=len(cdp_urls),
		api_mode=model['api_mode'],  # type: ignore[arg-type]
		reasoning_effort=model['reasoning_effort'],  # type: ignore[arg-type]
		browser_driver=BrowserDriver.PLAYWRIGHT,
		rerun_failed=False,
		cdp_headers_provider=header_provider,
	)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Run the official WebRetriever submission interface.')
	parser.add_argument('task_file', type=Path)
	parser.add_argument('output_dir', type=Path)
	parser.add_argument('cdp_urls', nargs='+', metavar='cdp_url')
	return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
	args = _parse_args(argv)
	try:
		config = build_submission_config(
			task_file=args.task_file,
			output_dir=args.output_dir,
			cdp_urls=args.cdp_urls,
		)
		asyncio.run(run(config))
	except (OSError, ValueError) as exc:
		print(f'WebRetriever submission failed: {type(exc).__name__}: {exc}')
		return 2
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
