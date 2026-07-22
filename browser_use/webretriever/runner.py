from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
from browser_use.webretriever.artifacts import TaskArtifactWriter, atomic_write_json
from browser_use.webretriever.browser import BrowserRuntime, cdp_headers_for_url, is_sec_url, redact_cdp_url
from browser_use.webretriever.models import CompetitionTask, load_tasks
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE, normalize_thought_language

ApiMode = Literal['auto', 'responses', 'chat-completions']
ReasoningEffort = Literal['low', 'medium', 'high']
DEFAULT_MAX_CONCURRENCY = 3
MAX_CONCURRENCY = 8
MAX_TASK_TIMEOUT_SECONDS = 300.0
_SEC_USER_AGENT_EMAIL_RE = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')
_MAX_SEC_USER_AGENT_LENGTH = 512


@dataclass(slots=True)
class RunnerConfig:
	input_path: Path
	output_dir: Path
	model: str
	api_key: str
	api_base: str | None
	cdp_urls: list[str]
	sec_user_agent: str | None = None
	vlm_ports: list[int] = field(default_factory=list)
	api_mode: ApiMode = 'auto'
	max_steps: int = 100
	model_timeout_seconds: float = 180.0
	task_timeout_seconds: float = 300.0
	max_concurrency: int = DEFAULT_MAX_CONCURRENCY
	reasoning_effort: ReasoningEffort = 'medium'
	thought_language: str = DEFAULT_THOUGHT_LANGUAGE
	local_browser: bool = False
	headless: bool = True
	rerun_failed: bool = False
	task_indices: frozenset[int] | None = None
	limit: int | None = None

	def validate(self) -> None:
		if not self.model.strip():
			raise ValueError('model must not be empty')
		if not self.api_key and not self.vlm_ports:
			raise ValueError('an OpenAI-compatible API key is required')
		if self.api_base and self.vlm_ports:
			raise ValueError('api_base and vlm_ports are mutually exclusive')
		if any(not 1 <= port <= 65535 for port in self.vlm_ports):
			raise ValueError('vlm_ports must contain valid TCP ports')
		if len(self.vlm_ports) > 8:
			raise ValueError('at most 8 local VLM endpoints may be configured')
		if not 1 <= self.max_steps <= 100:
			raise ValueError('max_steps must be between 1 and the competition limit of 100')
		if not 0 < self.model_timeout_seconds <= 180:
			raise ValueError('model_timeout_seconds must be in (0, 180]')
		if self.task_timeout_seconds <= 0:
			raise ValueError('task_timeout_seconds must be greater than 0')
		if self.task_timeout_seconds > MAX_TASK_TIMEOUT_SECONDS:
			raise ValueError(f'task_timeout_seconds must be at most {MAX_TASK_TIMEOUT_SECONDS:g}')
		if not 1 <= self.max_concurrency <= MAX_CONCURRENCY:
			raise ValueError(f'max_concurrency must be between 1 and the competition limit of {MAX_CONCURRENCY}')
		if self.local_browser and self.cdp_urls:
			raise ValueError('local_browser and cdp_urls are mutually exclusive')
		if not self.local_browser and not self.cdp_urls:
			raise ValueError('provide at least one CDP URL, or explicitly use local_browser')
		if self.rerun_failed and not self.local_browser:
			raise ValueError('rerun_failed is development-only; formal CDP runs must not retry failed tasks')
		if len(self.cdp_urls) > 8:
			raise ValueError('the competition permits at most 8 concurrent CDP browsers')
		if self.limit is not None and self.limit < 1:
			raise ValueError('limit must be at least 1')
		self.sec_user_agent = normalize_sec_user_agent(self.sec_user_agent)
		self.thought_language = normalize_thought_language(self.thought_language)
		validate_model_policy(self.model)


def _version_tuple(match: re.Match[str]) -> tuple[int, int]:
	return int(match.group(1)), int(match.group(2) or 0)


def normalize_sec_user_agent(value: str | None) -> str | None:
	"""Validate the declared identity required by SEC automated-access policy."""

	if value is None:
		return None
	if any(character in value for character in '\r\n\x00') or not value.isprintable():
		raise ValueError('WEBRETRIEVER_SEC_USER_AGENT must be a single printable line')
	normalized = value.strip()
	if not normalized:
		return None
	if len(normalized) > _MAX_SEC_USER_AGENT_LENGTH:
		raise ValueError(f'WEBRETRIEVER_SEC_USER_AGENT must be at most {_MAX_SEC_USER_AGENT_LENGTH} characters')
	try:
		normalized.encode('ascii')
	except UnicodeEncodeError as exc:
		raise ValueError('WEBRETRIEVER_SEC_USER_AGENT must use ASCII characters') from exc
	email_match = _SEC_USER_AGENT_EMAIL_RE.search(normalized)
	if email_match is None:
		raise ValueError('WEBRETRIEVER_SEC_USER_AGENT must include a contact email address')
	if not (normalized[: email_match.start()] + normalized[email_match.end() :]).strip():
		raise ValueError('WEBRETRIEVER_SEC_USER_AGENT must include an organization name and contact email address')
	return normalized


def validate_model_policy(model: str) -> None:
	"""Reject only model versions that are unambiguously above published caps."""
	name = model.lower()
	checks: list[tuple[str, str, tuple[int, int]]] = [
		('OpenAI', r'(?<![a-z])gpt-(\d+)(?:\.(\d+))?', (5, 4)),
		('Google', r'gemini-(\d+)(?:\.(\d+))?', (3, 1)),
		('xAI', r'grok-(\d+)(?:\.(\d+))?', (4, 3)),
	]
	for provider, pattern, maximum in checks:
		match = re.search(pattern, name)
		if match and _version_tuple(match) > maximum:
			raise ValueError(f'{provider} model {model!r} is above the challenge maximum version {maximum[0]}.{maximum[1]}')

	# Anthropic names generally encode the version as claude-...-4-6.
	claude_match = re.search(r'claude(?:-[a-z]+)*-(\d+)[.-](\d+)(?:\b|$)', name)
	if claude_match and _version_tuple(claude_match) > (4, 6):
		raise ValueError(f'Anthropic model {model!r} is above the challenge maximum version 4.6')
	claude_major = re.search(r'claude(?:-[a-z]+)*-(\d+)(?:\b|$)', name)
	if claude_major and int(claude_major.group(1)) > 4:
		raise ValueError(f'Anthropic model {model!r} is above the challenge maximum version 4.6')

	glm_match = re.search(r'(?<![a-z])glm-(\d+)', name)
	if glm_match and int(glm_match.group(1)) > 5:
		raise ValueError(f'Zhipu model {model!r} is above the challenge maximum GLM-5V-Turbo family')

	kimi_match = re.search(r'kimi[-_]?k?(\d+)(?:\.(\d+))?', name)
	if kimi_match and _version_tuple(kimi_match) > (2, 6):
		raise ValueError(f'Moonshot model {model!r} is above the challenge maximum version Kimi-K2.6')


def resolve_responses_api(mode: ApiMode) -> bool:
	if mode == 'responses':
		return True
	if mode == 'chat-completions':
		return False
	configured = os.getenv('WEBRETRIEVER_API_MODE', '').strip().lower()
	if configured in {'responses', 'response'}:
		return True
	if configured in {'chat', 'chat-completions', 'chat_completions'}:
		return False
	# This workspace's documented LiteLLM gateway is Responses-only.
	return bool(os.getenv('LITELLM_BASE_URL') or os.getenv('LITELLM_MASTER_KEY'))


def build_llm(config: RunnerConfig, worker_id: int = 0) -> ChatOpenAI:
	base_url = config.api_base
	api_key = config.api_key
	use_responses_api = resolve_responses_api(config.api_mode)
	if config.vlm_ports:
		base_url = f'http://127.0.0.1:{config.vlm_ports[worker_id % len(config.vlm_ports)]}/v1'
		api_key = api_key or 'not-required'
		if config.api_mode == 'auto':
			use_responses_api = False
	return ChatOpenAI(
		model=config.model,
		api_key=api_key,
		base_url=base_url,
		use_responses_api=use_responses_api,
		stream_responses_api=use_responses_api,
		timeout=config.model_timeout_seconds,
		max_retries=0,
		temperature=0.1,
		reasoning_effort=config.reasoning_effort,
		max_completion_tokens=4096,
	)


def _load_existing_status(writer: TaskArtifactWriter) -> str | None:
	try:
		with writer.result_path.open(encoding='utf-8') as result_file:
			payload = json.load(result_file)
	except (FileNotFoundError, OSError, json.JSONDecodeError):
		return None
	status = payload.get('status')
	return status if isinstance(status, str) else None


def _should_skip(status: str | None, *, rerun_failed: bool) -> bool:
	if status is None or status == 'PENDING':
		return False
	if status == 'SUCCESS':
		return True
	return not rerun_failed


def _clear_previous_trajectory(writer: TaskArtifactWriter) -> None:
	for directory in (writer.trajectory_dir, writer.trajectory_visual_dir):
		if not directory.exists():
			continue
		for child in directory.iterdir():
			if child.is_file() or child.is_symlink():
				child.unlink()
			elif child.is_dir():
				shutil.rmtree(child)


def _worker_logger(output_dir: Path, worker_id: int) -> logging.Logger:
	logger = logging.getLogger(f'webretriever.worker.{worker_id}')
	logger.setLevel(logging.INFO)
	logger.propagate = False
	if logger.handlers:
		return logger
	log_dir = output_dir / 'logs'
	log_dir.mkdir(parents=True, exist_ok=True)
	formatter = logging.Formatter(
		f'%(asctime)s %(levelname)s [worker {worker_id}] %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S',
	)
	file_handler = logging.FileHandler(log_dir / f'worker_{worker_id}_{datetime.now().strftime("%Y%m%d")}.log', encoding='utf-8')
	file_handler.setFormatter(formatter)
	console_handler = logging.StreamHandler()
	console_handler.setFormatter(formatter)
	logger.addHandler(file_handler)
	logger.addHandler(console_handler)
	return logger


def _result_payload(
	task: CompetitionTask,
	outcome: AgentRunOutcome,
	*,
	urls: list[str],
	model: str,
	thought_language: str = DEFAULT_THOUGHT_LANGUAGE,
	task_started_at: datetime | None = None,
	task_elapsed_seconds: float = 0.0,
	task_timeout_seconds: float | None = None,
) -> dict[str, Any]:
	payload: dict[str, Any] = {
		**task.prompt_payload(),
		'status': outcome.status,
		'actions': outcome.actions,
		'thoughts': outcome.thoughts,
		'urls': urls,
		'agent_answer': outcome.agent_answer,
		'evidence': outcome.evidence,
		'steps': outcome.steps,
		'error': outcome.error,
		'duration_seconds': outcome.duration_seconds,
		'model': model,
		'thought_language': thought_language,
		'usage': outcome.usage,
		# ``duration_seconds`` predates the task watchdog and measures only the
		# agent loop.  Keep it for compatibility while exposing the end-to-end
		# task timing used for the timeout decision.
		'task_elapsed_seconds': round(task_elapsed_seconds, 3),
		'task_completed_at': datetime.now(timezone.utc).isoformat(),
	}
	if task_started_at is not None:
		payload['task_started_at'] = task_started_at.isoformat()
	if task_timeout_seconds is not None:
		payload['task_timeout_seconds'] = task_timeout_seconds
	return payload


def _consume_detached_task_result(task: asyncio.Future[Any]) -> None:
	"""Retrieve a late cancellation-resistant result without warning."""

	if task.cancelled():
		return
	try:
		task.exception()
	except BaseException:
		pass


async def _await_with_hard_timeout(awaitable: Any, timeout_seconds: float) -> Any:
	"""Enforce a deadline without waiting for cancellation acknowledgement."""

	task = asyncio.ensure_future(awaitable)
	try:
		done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
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


def _remaining_task_seconds(started_monotonic: float, timeout_seconds: float) -> float:
	remaining = timeout_seconds - (time.monotonic() - started_monotonic)
	if remaining <= 0:
		raise TimeoutError
	return remaining


def _task_timeout_outcome(agent: ProtocolIIIAgent | None, timeout_seconds: float) -> AgentRunOutcome:
	"""Turn a task deadline into a failure without discarding completed work."""

	outcome = agent.partial_outcome if agent is not None else None
	if outcome is None:
		outcome = AgentRunOutcome(status='FAIL_TASK_TIMEOUT')
	outcome.status = 'FAIL_TASK_TIMEOUT'
	outcome.error = f'Task exceeded the {timeout_seconds:g}-second time limit'
	return outcome


async def _run_task(
	*,
	context: BrowserContext,
	task: CompetitionTask,
	config: RunnerConfig,
	llm: ChatOpenAI,
	logger: logging.Logger,
) -> str:
	writer = TaskArtifactWriter(config.output_dir, task)
	writer.prepare()
	if not writer.acquire_lock(blocking=False):
		logger.info('Skipping task %s/%s because another worker owns its lock', task.task_idx, task.task_id)
		return 'LOCKED'

	try:
		# Re-read only after acquiring ownership, so two runners cannot both pass
		# the resume check and execute the same formal task.
		existing_status = _load_existing_status(writer)
		if _should_skip(existing_status, rerun_failed=config.rerun_failed):
			logger.info('Skipping task %s/%s with existing status %s', task.task_idx, task.task_id, existing_status)
			return existing_status or 'SKIPPED'

		task_started_at = datetime.now(timezone.utc)
		task_started_monotonic = time.monotonic()
		runtime: BrowserRuntime | None = None
		agent: ProtocolIIIAgent | None = None
		try:
			if config.rerun_failed and existing_status not in {None, 'PENDING'}:
				_clear_previous_trajectory(writer)
			writer.write_result(status='PENDING', actions=[], thoughts=[], urls=[], agent_answer='')
			logger.info('Starting task %s/%s at %s', task.task_idx, task.task_id, task.website)
			is_sec_task = is_sec_url(task.website)
			if is_sec_task and config.sec_user_agent is None:
				logger.warning(
					'SEC task %s/%s is running without a declared User-Agent; '
					'set WEBRETRIEVER_SEC_USER_AGENT to an organization name and contact email to avoid SEC blocking',
					task.task_idx,
					task.task_id,
				)

			runtime = BrowserRuntime(
				context,
				writer.task_dir,
				logger,
				declared_user_agent=config.sec_user_agent if is_sec_task else None,
			)
			await _await_with_hard_timeout(
				runtime.start(task.website),
				_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
			)
			agent = ProtocolIIIAgent(
				task=task,
				llm=llm,
				runtime=runtime,
				task_dir=writer.task_dir,
				max_steps=config.max_steps,
				model_timeout_seconds=config.model_timeout_seconds,
				thought_language=config.thought_language,
				task_deadline_monotonic=task_started_monotonic + config.task_timeout_seconds,
			)
			outcome = await _await_with_hard_timeout(
				agent.run(),
				_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
			)
		except TimeoutError:
			outcome = _task_timeout_outcome(agent, config.task_timeout_seconds)
		except Exception as exc:
			logger.exception('Task %s/%s crashed', task.task_idx, task.task_id)
			outcome = AgentRunOutcome(
				status='FAIL_RUNTIME',
				error=f'{type(exc).__name__}: {exc}',
			)
		finally:
			if runtime is not None:
				try:
					await runtime.close()
				except Exception as exc:
					logger.warning('Runtime cleanup failed for task %s: %s', task.task_id, exc)

		urls = list(runtime.visited_urls) if runtime is not None else []
		capture = runtime.capture_payload() if runtime is not None else None
		writer.write_capture(capture)
		writer.write_result(
			_result_payload(
				task,
				outcome,
				urls=urls,
				model=config.model,
				thought_language=config.thought_language,
				task_started_at=task_started_at,
				task_elapsed_seconds=time.monotonic() - task_started_monotonic,
				task_timeout_seconds=config.task_timeout_seconds,
			)
		)
		logger.info('Finished task %s/%s with status %s', task.task_idx, task.task_id, outcome.status)
		return outcome.status
	finally:
		writer.release_lock()


async def _consume_tasks(
	*,
	worker_id: int,
	context: BrowserContext,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	llm: ChatOpenAI,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
) -> None:
	logger = _worker_logger(config.output_dir, worker_id)
	while True:
		try:
			task = queue.get_nowait()
		except asyncio.QueueEmpty:
			return
		try:
			is_sec_task = is_sec_url(task.website)
			acquired_sec_slot = False
			try:
				if is_sec_task:
					await sec_task_semaphore.acquire()
					acquired_sec_slot = True
				statuses[task.task_id] = await _run_task(
					context=context,
					task=task,
					config=config,
					llm=llm,
					logger=logger,
				)
			except Exception as exc:
				# Artifact I/O and other runner-level failures must not abandon the
				# remainder of this worker's one-shot task shard.
				logger.exception('Runner failed while finalizing task %s/%s', task.task_idx, task.task_id)
				failure = AgentRunOutcome(status='FAIL_RUNNER', error=f'{type(exc).__name__}: {exc}')
				statuses[task.task_id] = failure.status
				try:
					writer = TaskArtifactWriter(config.output_dir, task)
					writer.write_result(_result_payload(task, failure, urls=[], model=config.model))
					writer.write_capture()
				except Exception:
					logger.exception('Could not persist runner failure for task %s/%s', task.task_idx, task.task_id)
			finally:
				if acquired_sec_slot:
					sec_task_semaphore.release()
		finally:
			queue.task_done()


async def _cdp_worker(
	*,
	worker_id: int,
	playwright: Playwright,
	cdp_url: str,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
) -> None:
	logger = _worker_logger(config.output_dir, worker_id)
	llm = build_llm(config, worker_id)
	logger.info('Connecting to CDP browser %s', redact_cdp_url(cdp_url))
	browser: Browser | None = None
	try:
		browser = await playwright.chromium.connect_over_cdp(
			cdp_url,
			headers=cdp_headers_for_url(cdp_url) or None,
			timeout=60_000,
		)
		context = browser.contexts[0] if browser.contexts else await browser.new_context(accept_downloads=True)
		await _consume_tasks(
			worker_id=worker_id,
			context=context,
			queue=queue,
			config=config,
			llm=llm,
			statuses=statuses,
			sec_task_semaphore=sec_task_semaphore,
		)
	except Exception as exc:
		# Playwright connection errors may repeat the endpoint verbatim.  Avoid
		# traceback logging here so evaluator access tokens never reach artifacts.
		logger.error('CDP worker failed for %s: %s', redact_cdp_url(cdp_url), redact_cdp_url(str(exc)))
	finally:
		if browser is not None:
			try:
				await browser.close()
			except Exception as exc:
				logger.warning('Could not close CDP connection: %s', redact_cdp_url(str(exc)))


async def _local_worker(
	*,
	worker_id: int,
	browser: Browser,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
) -> None:
	"""Run one isolated local browser context for each concurrent worker."""

	logger = _worker_logger(config.output_dir, worker_id)
	llm = build_llm(config, worker_id)
	context: BrowserContext | None = None
	try:
		context = await browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 900})
		await _consume_tasks(
			worker_id=worker_id,
			context=context,
			queue=queue,
			config=config,
			llm=llm,
			statuses=statuses,
			sec_task_semaphore=sec_task_semaphore,
		)
	except Exception:
		logger.exception('Local browser worker failed')
	finally:
		if context is not None:
			try:
				await context.close()
			except Exception as exc:
				logger.warning('Could not close local browser context: %s', exc)


def _select_tasks(tasks: list[CompetitionTask], config: RunnerConfig) -> list[CompetitionTask]:
	selected = tasks
	if config.task_indices is not None:
		selected = [task for task in selected if task.task_idx in config.task_indices]
	if config.limit is not None:
		selected = selected[: config.limit]
	return selected


async def run(config: RunnerConfig) -> dict[str, Any]:
	config.validate()
	tasks = _select_tasks(load_tasks(config.input_path), config)
	config.output_dir.mkdir(parents=True, exist_ok=True)
	if not tasks:
		raise ValueError('no tasks selected')

	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	for task in tasks:
		queue.put_nowait(task)
	statuses: dict[str, str] = {}
	sec_task_semaphore = asyncio.Semaphore(1)
	worker_count = min(config.max_concurrency, len(tasks))
	async with async_playwright() as playwright:
		if config.local_browser:
			browser = await playwright.chromium.launch(headless=config.headless)
			try:
				await asyncio.gather(
					*(
						_local_worker(
							worker_id=worker_id,
							browser=browser,
							queue=queue,
							config=config,
							statuses=statuses,
							sec_task_semaphore=sec_task_semaphore,
						)
						for worker_id in range(worker_count)
					)
				)
			finally:
				await browser.close()
		else:
			worker_urls = config.cdp_urls[:worker_count]
			worker_count = len(worker_urls)
			await asyncio.gather(
				*(
					_cdp_worker(
						worker_id=worker_id,
						playwright=playwright,
						cdp_url=cdp_url,
						queue=queue,
						config=config,
						statuses=statuses,
						sec_task_semaphore=sec_task_semaphore,
					)
					for worker_id, cdp_url in enumerate(worker_urls)
				)
			)

	# A connection failure must still produce one diagnostic result per task.
	while not queue.empty():
		task = queue.get_nowait()
		writer = TaskArtifactWriter(config.output_dir, task)
		writer.prepare()
		failure = AgentRunOutcome(status='FAIL_BROWSER_CONNECT', error='No CDP worker was available for this task')
		writer.write_result(_result_payload(task, failure, urls=[], model=config.model))
		writer.write_capture()
		statuses[task.task_id] = failure.status
		queue.task_done()

	counts: dict[str, int] = {}
	for status in statuses.values():
		counts[status] = counts.get(status, 0) + 1
	summary = {
		'created_at': datetime.now(timezone.utc).isoformat(),
		'input': str(config.input_path),
		'total_selected': len(tasks),
		'max_concurrency': config.max_concurrency,
		'workers_started': worker_count,
		'counts': counts,
		'statuses': statuses,
	}
	atomic_write_json(config.output_dir / 'logs' / 'summary.json', summary)
	return summary


__all__ = [
	'DEFAULT_MAX_CONCURRENCY',
	'MAX_CONCURRENCY',
	'RunnerConfig',
	'build_llm',
	'normalize_sec_user_agent',
	'resolve_responses_api',
	'run',
	'validate_model_policy',
]
