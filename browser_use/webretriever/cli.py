"""Command-line entry point for the WebRetriever Protocol III runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from browser_use.webretriever.configuration import ConfigurationError, FileConfiguration, load_file_configuration
from browser_use.webretriever.models import load_tasks
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE
from browser_use.webretriever.runner import (
	DEFAULT_MAX_CONCURRENCY,
	DEFAULT_TASK_TIMEOUT_SECONDS,
	MAX_CONCURRENCY,
	BrowserDriver,
	RunnerConfig,
	run,
	run_patchright_experiment,
	run_rebrowser_experiment,
)


def _first_env(*names: str) -> str | None:
	for name in names:
		value = os.getenv(name)
		if value is not None and value.strip():
			return value.strip()
	return None


def _sec_user_agent_from_env() -> str | None:
	"""Read the SEC identity without normalizing potentially unsafe header input."""

	value = os.getenv('WEBRETRIEVER_SEC_USER_AGENT')
	return value if value is not None and value.strip() else None


def _split_cdp_urls(values: Sequence[str] | None) -> list[str]:
	if values is not None:
		raw_values = values
	else:
		configured = _first_env('WEBRETRIEVER_CDP_URLS', 'WEBRETRIEVER_CDP_URL', 'CDP_URL')
		raw_values = [configured] if configured else []

	urls: list[str] = []
	for raw_value in raw_values:
		for candidate in re.split(r'[\s,]+', raw_value.strip()):
			if candidate and candidate not in urls:
				urls.append(candidate)
	return urls


def _parse_task_indices(values: Sequence[str] | None) -> frozenset[int] | None:
	if not values:
		return None
	indices: set[int] = set()
	for raw_value in values:
		for token in raw_value.split(','):
			token = token.strip()
			if not token:
				continue
			if '-' not in token:
				index = int(token)
				if index < 0:
					raise ValueError('task indices must be non-negative')
				indices.add(index)
				continue
			start_text, end_text = token.split('-', 1)
			start, end = int(start_text), int(end_text)
			if start < 0 or end < start:
				raise ValueError(f'invalid task index range: {token!r}')
			indices.update(range(start, end + 1))
	return frozenset(indices)


def _setting(configuration: FileConfiguration | None, key: str, default: Any) -> Any:
	return configuration.get(key, default) if configuration is not None else default


def _task_index_default(configuration: FileConfiguration | None) -> list[str] | None:
	value = _setting(configuration, 'task_indices', None)
	if value is None:
		return None
	if isinstance(value, str):
		return [value]
	return [str(item) for item in value]


class _TaskIndexOverrideAction(argparse.Action):
	"""Append command-line task selections without retaining file defaults."""

	def __call__(
		self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: str, option_string: str | None = None
	) -> None:
		if not getattr(namespace, '_task_index_overridden', False):
			setattr(namespace, self.dest, [])
			setattr(namespace, '_task_index_overridden', True)
		current = getattr(namespace, self.dest)
		current.append(values)


def build_parser(configuration: FileConfiguration | None = None) -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description='Run the Playwright-only WebRetriever Protocol III agent.',
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	parser.add_argument('--config', type=Path, help='TOML file containing a [webretriever] configuration section')
	parser.add_argument(
		'--input',
		'--task-file',
		'--task_file',
		dest='input_path',
		type=Path,
		default=_setting(configuration, 'input_path', None),
		help='challenge task JSON/JSONL file',
	)
	parser.add_argument(
		'--output',
		'--output-dir',
		'--output_dir',
		dest='output_dir',
		type=Path,
		default=_setting(configuration, 'output_dir', None),
	)
	parser.add_argument(
		'--cdp_url',
		'--cdp-url',
		'--cdp-urls',
		dest='cdp_urls',
		nargs='+',
		default=_setting(configuration, 'cdp_urls', None),
		help='one or more evaluator-provided CDP URLs; may also be set in WEBRETRIEVER_CDP_URLS',
	)

	parser.add_argument('--model', default=_setting(configuration, 'model', None), help='OpenAI-compatible model name')
	parser.add_argument(
		'--api_base',
		'--api-base',
		dest='api_base',
		default=_setting(configuration, 'api_base', None),
		help='OpenAI-compatible API base URL',
	)
	parser.add_argument(
		'--api_key',
		'--api-key',
		dest='api_key',
		default=_setting(configuration, 'api_key', None),
		help='OpenAI-compatible API key',
	)
	parser.add_argument(
		'--sec-user-agent',
		default=_setting(configuration, 'sec_user_agent', None),
		help='SEC organization/contact identity; normally set WEBRETRIEVER_SEC_USER_AGENT instead',
	)
	parser.add_argument(
		'--vlm_ports',
		'--vlm-ports',
		dest='vlm_ports',
		type=int,
		nargs='+',
		default=_setting(configuration, 'vlm_ports', []),
		help='local OpenAI-compatible vLLM ports, assigned round-robin to workers',
	)
	parser.add_argument(
		'--api-mode',
		choices=('auto', 'responses', 'chat-completions'),
		default=_setting(configuration, 'api_mode', 'auto'),
		help='OpenAI-compatible endpoint dialect',
	)
	parser.add_argument(
		'--reasoning-effort',
		choices=('low', 'medium', 'high'),
		default=_setting(configuration, 'reasoning_effort', _first_env('WEBRETRIEVER_REASONING_EFFORT') or 'low'),
		help='reasoning effort; defaults to the configuration file, WEBRETRIEVER_REASONING_EFFORT, or low',
	)
	parser.add_argument(
		'--thought-language',
		default=_setting(
			configuration, 'thought_language', _first_env('WEBRETRIEVER_THOUGHT_LANGUAGE') or DEFAULT_THOUGHT_LANGUAGE
		),
		help='language used for each model-generated thought',
	)
	parser.add_argument(
		'--structured-prompt-log',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'structured_prompt_log', False),
		help='write the optional detailed prompt-trace schema instead of the default line-oriented prompt log',
	)
	parser.add_argument(
		'--max-steps', type=int, default=_setting(configuration, 'max_steps', 100), help='hard-capped by the rules at 100'
	)
	parser.add_argument(
		'--model-timeout',
		type=float,
		default=_setting(configuration, 'model_timeout_seconds', 180.0),
		help='seconds; hard-capped by the rules at 180',
	)
	parser.add_argument(
		'--task-timeout',
		type=float,
		default=_setting(configuration, 'task_timeout_seconds', DEFAULT_TASK_TIMEOUT_SECONDS),
		help='seconds allowed for one complete task; must be greater than zero',
	)
	parser.add_argument(
		'--max-concurrency',
		type=int,
		default=_setting(configuration, 'max_concurrency', DEFAULT_MAX_CONCURRENCY),
		help=f'number of tasks to run concurrently; hard-capped by the rules at {MAX_CONCURRENCY}',
	)

	parser.add_argument(
		'--task-index',
		action=_TaskIndexOverrideAction,
		default=_task_index_default(configuration),
		help='local/debug selection, e.g. --task-index 3 or --task-index 1,4-6',
	)
	parser.add_argument(
		'--limit',
		type=int,
		default=_setting(configuration, 'limit', None),
		help='local/debug limit after task-index filtering',
	)
	parser.add_argument(
		'--local-browser',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'local_browser', False),
		help='launch local Playwright Chromium instead of evaluator CDP (development only)',
	)
	parser.add_argument(
		'--headed',
		action=argparse.BooleanOptionalAction,
		default=not bool(_setting(configuration, 'headless', True)),
		help='show the local development browser',
	)
	parser.add_argument(
		'--rerun-failed',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'rerun_failed', False),
		help='replace failed artifacts (local-browser development only; prohibited in a formal run)',
	)
	parser.add_argument(
		'--browser-driver',
		choices=tuple(driver.value for driver in BrowserDriver),
		default=_setting(configuration, 'browser_driver', BrowserDriver.PLAYWRIGHT.value),
		help='Playwright-compatible client used to attach to a CDP browser',
	)
	parser.add_argument(
		'--patchright-qualification-report',
		type=Path,
		default=_setting(configuration, 'patchright_qualification_report', None),
		help='passing experiment_summary.json required before Patchright is used outside experiment mode',
	)
	parser.add_argument(
		'--patchright-experiment',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'patchright_experiment', False),
		help='run the two-round AB/BA experiment across the configured local CDP endpoints',
	)
	parser.add_argument(
		'--rebrowser-qualification-report',
		type=Path,
		default=_setting(configuration, 'rebrowser_qualification_report', None),
		help='passing one-endpoint Rebrowser experiment_summary.json required before a formal Rebrowser CDP run',
	)
	parser.add_argument(
		'--rebrowser-experiment',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'rebrowser_experiment', False),
		help='run the one-endpoint, one-round Playwright/Rebrowser comparison',
	)
	parser.add_argument(
		'--validate-only',
		action=argparse.BooleanOptionalAction,
		default=_setting(configuration, 'validate_only', False),
		help='validate and summarize the task file without opening a browser or requiring model credentials',
	)
	return parser


def _validated_task_summary(input_path: Path) -> dict[str, object]:
	tasks = load_tasks(input_path)
	return {
		'input': str(input_path),
		'task_count': len(tasks),
		'task_indices': [task.task_idx for task in tasks],
		'unique_websites': len({task.website for task in tasks}),
		'ground_truth_exposed_to_agent': False,
	}


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> RunnerConfig:
	try:
		task_indices = _parse_task_indices(args.task_index)
	except ValueError as exc:
		parser.error(str(exc))

	if args.input_path is None:
		parser.error('provide --input or set input_path in the TOML configuration file')
	if args.output_dir is None:
		parser.error('provide --output or set output_dir in the TOML configuration file')
	model = args.model or _first_env('WEBRETRIEVER_MODEL', 'LITELLM_MODEL', 'OPENAI_MODEL')
	if args.vlm_ports and args.api_base:
		parser.error('--vlm_ports and --api-base are mutually exclusive')
	api_key = args.api_key or _first_env('WEBRETRIEVER_API_KEY', 'LITELLM_MASTER_KEY', 'OPENAI_API_KEY')
	api_base = (
		None if args.vlm_ports else args.api_base or _first_env('WEBRETRIEVER_API_BASE', 'LITELLM_BASE_URL', 'OPENAI_BASE_URL')
	)
	if not model:
		parser.error('provide --model or WEBRETRIEVER_MODEL/LITELLM_MODEL/OPENAI_MODEL')
	if not api_key and not args.vlm_ports:
		parser.error('provide --api-key or WEBRETRIEVER_API_KEY/LITELLM_MASTER_KEY/OPENAI_API_KEY')

	config = RunnerConfig(
		input_path=args.input_path,
		output_dir=args.output_dir,
		model=model,
		api_key=api_key or '',
		api_base=api_base,
		cdp_urls=_split_cdp_urls(args.cdp_urls),
		sec_user_agent=args.sec_user_agent or _sec_user_agent_from_env(),
		vlm_ports=args.vlm_ports,
		api_mode=args.api_mode,
		max_steps=args.max_steps,
		model_timeout_seconds=args.model_timeout,
		task_timeout_seconds=args.task_timeout,
		max_concurrency=args.max_concurrency,
		reasoning_effort=args.reasoning_effort,
		thought_language=args.thought_language,
		structured_prompt_log=args.structured_prompt_log,
		local_browser=args.local_browser,
		headless=not args.headed,
		rerun_failed=args.rerun_failed,
		task_indices=task_indices,
		limit=args.limit,
		browser_driver=BrowserDriver(args.browser_driver),
		experiment_mode=bool(args.patchright_experiment or args.rebrowser_experiment),
		patchright_qualification_report=args.patchright_qualification_report,
		rebrowser_qualification_report=args.rebrowser_qualification_report,
	)
	try:
		config.validate()
	except ValueError as exc:
		parser.error(str(exc))
	return config


def _configuration_from_argv(argv: Sequence[str]) -> FileConfiguration | None:
	config_parser = argparse.ArgumentParser(add_help=False)
	config_parser.add_argument('--config', type=Path)
	options, _ = config_parser.parse_known_args(argv)
	if options.config is None:
		return None
	return load_file_configuration(options.config)


def main(argv: Sequence[str] | None = None) -> int:
	load_dotenv()
	raw_argv = list(sys.argv[1:] if argv is None else argv)
	try:
		configuration = _configuration_from_argv(raw_argv)
	except ConfigurationError as exc:
		build_parser().error(str(exc))
	parser = build_parser(configuration)
	args = parser.parse_args(raw_argv)
	if args.validate_only:
		if args.input_path is None:
			parser.error('provide --input or set input_path in the TOML configuration file')
		try:
			summary = _validated_task_summary(args.input_path)
		except ValueError as exc:
			parser.error(str(exc))
		print(json.dumps(summary, ensure_ascii=False, indent=2))
		return 0

	config = config_from_args(args, parser)
	if args.patchright_experiment and args.rebrowser_experiment:
		parser.error('--patchright-experiment and --rebrowser-experiment are mutually exclusive')
	try:
		if args.patchright_experiment:
			summary = asyncio.run(run_patchright_experiment(config)).to_dict()
		elif args.rebrowser_experiment:
			summary = asyncio.run(run_rebrowser_experiment(config)).to_dict()
		else:
			summary = asyncio.run(run(config))
	except KeyboardInterrupt:
		print('Interrupted; completed task artifacts remain resumable.', file=sys.stderr)
		return 130
	except Exception as exc:
		print(f'WebRetriever runner failed: {type(exc).__name__}: {exc}', file=sys.stderr)
		return 1
	print(json.dumps(summary, ensure_ascii=False, indent=2))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())


__all__ = ['build_parser', 'config_from_args', 'main']
