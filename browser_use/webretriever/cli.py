"""Command-line entry point for the WebRetriever Protocol III runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from browser_use.webretriever.models import load_tasks
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE
from browser_use.webretriever.runner import (
	DEFAULT_MAX_CONCURRENCY,
	MAX_CONCURRENCY,
	MAX_TASK_TIMEOUT_SECONDS,
	RunnerConfig,
	run,
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
	if values:
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


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description='Run the Playwright-only WebRetriever Protocol III agent.',
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	parser.add_argument(
		'--input',
		'--task-file',
		'--task_file',
		dest='input_path',
		type=Path,
		required=True,
		help='challenge task JSON/JSONL file',
	)
	parser.add_argument('--output', '--output-dir', '--output_dir', dest='output_dir', type=Path, required=True)
	parser.add_argument(
		'--cdp_url',
		'--cdp-url',
		'--cdp-urls',
		dest='cdp_urls',
		nargs='+',
		help='one or more evaluator-provided CDP URLs; may also be set in WEBRETRIEVER_CDP_URLS',
	)

	parser.add_argument('--model', help='OpenAI-compatible model name')
	parser.add_argument('--api_base', '--api-base', dest='api_base', help='OpenAI-compatible API base URL')
	parser.add_argument('--api_key', '--api-key', dest='api_key', help='OpenAI-compatible API key')
	parser.add_argument(
		'--vlm_ports',
		'--vlm-ports',
		dest='vlm_ports',
		type=int,
		nargs='+',
		default=[],
		help='local OpenAI-compatible vLLM ports, assigned round-robin to workers',
	)
	parser.add_argument(
		'--api-mode',
		choices=('auto', 'responses', 'chat-completions'),
		default='auto',
		help='OpenAI-compatible endpoint dialect',
	)
	parser.add_argument('--reasoning-effort', choices=('low', 'medium', 'high'), default='medium')
	parser.add_argument(
		'--thought-language',
		help='language used for each model-generated thought; defaults to WEBRETRIEVER_THOUGHT_LANGUAGE or Chinese',
	)
	parser.add_argument('--max-steps', type=int, default=100, help='hard-capped by the rules at 100')
	parser.add_argument('--model-timeout', type=float, default=180.0, help='seconds; hard-capped by the rules at 180')
	parser.add_argument(
		'--task-timeout',
		type=float,
		default=MAX_TASK_TIMEOUT_SECONDS,
		help=f'seconds allowed for one complete task; hard-capped at {MAX_TASK_TIMEOUT_SECONDS:g}',
	)
	parser.add_argument(
		'--max-concurrency',
		type=int,
		default=DEFAULT_MAX_CONCURRENCY,
		help=f'number of tasks to run concurrently; hard-capped by the rules at {MAX_CONCURRENCY}',
	)

	parser.add_argument(
		'--task-index',
		action='append',
		help='local/debug selection, e.g. --task-index 3 or --task-index 1,4-6',
	)
	parser.add_argument('--limit', type=int, help='local/debug limit after task-index filtering')
	parser.add_argument(
		'--local-browser',
		action='store_true',
		help='launch local Playwright Chromium instead of evaluator CDP (development only)',
	)
	parser.add_argument('--headed', action='store_true', help='show the local development browser')
	parser.add_argument(
		'--rerun-failed',
		action='store_true',
		help='replace failed artifacts (local-browser development only; prohibited in a formal run)',
	)
	parser.add_argument(
		'--validate-only',
		action='store_true',
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
		sec_user_agent=_sec_user_agent_from_env(),
		vlm_ports=args.vlm_ports,
		api_mode=args.api_mode,
		max_steps=args.max_steps,
		model_timeout_seconds=args.model_timeout,
		task_timeout_seconds=args.task_timeout,
		max_concurrency=args.max_concurrency,
		reasoning_effort=args.reasoning_effort,
		thought_language=args.thought_language or _first_env('WEBRETRIEVER_THOUGHT_LANGUAGE') or DEFAULT_THOUGHT_LANGUAGE,
		local_browser=args.local_browser,
		headless=not args.headed,
		rerun_failed=args.rerun_failed,
		task_indices=task_indices,
		limit=args.limit,
	)
	try:
		config.validate()
	except ValueError as exc:
		parser.error(str(exc))
	return config


def main(argv: Sequence[str] | None = None) -> int:
	load_dotenv()
	parser = build_parser()
	args = parser.parse_args(argv)
	if args.validate_only:
		try:
			summary = _validated_task_summary(args.input_path)
		except ValueError as exc:
			parser.error(str(exc))
		print(json.dumps(summary, ensure_ascii=False, indent=2))
		return 0

	config = config_from_args(args, parser)
	try:
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
