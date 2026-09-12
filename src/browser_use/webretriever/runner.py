from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from playwright.async_api import Browser, BrowserContext, async_playwright

from browser_use.llm.base import BaseChatModel
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
from browser_use.webretriever.artifacts import TaskArtifactWriter, atomic_write_json
from browser_use.webretriever.browser import (
	BrowserRuntime,
	cdp_headers_for_url,
	is_browser_session_closed_error,
	is_sec_url,
	redact_cdp_url,
)
from browser_use.webretriever.browser_failures import BrowserFailurePhase, classify_browser_failure
from browser_use.webretriever.browser_session import (
	BrowserRecoveryDeadlineExceeded,
	CdpWorkerSession,
	TaskBrowserRequest,
)
from browser_use.webretriever.completion import valid_completion_receipt
from browser_use.webretriever.connection import BrowserConnector, BrowserDriver
from browser_use.webretriever.experiment import (
	PATCHRIGHT_EXPERIMENT_TASK_INDICES,
	REBROWSER_EXPERIMENT_TASK_INDICES,
	ExperimentRecord,
	ExperimentSummary,
	patchright_qualification_report_passes,
	rebrowser_qualification_report_passes,
	write_experiment_summary,
)
from browser_use.webretriever.model_services import ModelServiceConfig, ModelServiceRouter
from browser_use.webretriever.models import CompetitionTask, load_tasks
from browser_use.webretriever.prompts import DEFAULT_THOUGHT_LANGUAGE, normalize_thought_language
from browser_use.webretriever.run_control import (
	CancellationToken,
	NoOpRunObserver,
	RunObserver,
	RunnerEvent,
	emit_safely,
)
from browser_use.webretriever.verification import VerificationState

ApiMode = Literal['auto', 'responses', 'chat-completions']
ReasoningEffort = Literal['low', 'medium', 'high']
DEFAULT_MAX_CONCURRENCY = 3
MAX_CONCURRENCY = 8
DEFAULT_TASK_TIMEOUT_SECONDS = 1200.0
TASK_FINALIZATION_GRACE_SECONDS = 60.0
DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES = PATCHRIGHT_EXPERIMENT_TASK_INDICES
DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES = REBROWSER_EXPERIMENT_TASK_INDICES
_SEC_USER_AGENT_EMAIL_RE = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')
_MAX_SEC_USER_AGENT_LENGTH = 512
_FAILURE_LOG_DETAIL_CHUNK_CHARS = 160
_FAILURE_LOG_DETAIL_MAX_CHARS = 2_400


@dataclass(slots=True)
class RunnerConfig:
	input_path: Path
	output_dir: Path
	model: str
	cdp_urls: list[str]
	model_services: list[ModelServiceConfig] = field(default_factory=list)
	sec_user_agent: str | None = None
	vlm_ports: list[int] = field(default_factory=list)
	api_mode: ApiMode = 'auto'
	max_steps: int = 100
	model_timeout_seconds: float = 180.0
	task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS
	max_concurrency: int = DEFAULT_MAX_CONCURRENCY
	reasoning_effort: ReasoningEffort = 'medium'
	thought_language: str = DEFAULT_THOUGHT_LANGUAGE
	structured_prompt_log: bool = False
	local_browser: bool = False
	headless: bool = True
	rerun_failed: bool = False
	task_indices: frozenset[int] | None = None
	limit: int | None = None
	browser_driver: BrowserDriver = BrowserDriver.PLAYWRIGHT
	experiment_mode: bool = False
	experiment_repeat_index: int = 0
	experiment_endpoint_label: str | None = None
	patchright_qualification_report: Path | None = None
	rebrowser_qualification_report: Path | None = None
	# The production CLI derives the Tencent sandbox header directly from its
	# endpoint.  A submission adapter may instead reuse the competition
	# template's supplied authentication helper without changing runner logic.
	cdp_headers_provider: Callable[[str], Mapping[str, str]] | None = None
	project_url: str | None = None

	def validate(self) -> None:
		if not self.model.strip():
			raise ValueError('model must not be empty')
		if self.model_services and self.vlm_ports:
			raise ValueError('model_services and vlm_ports are mutually exclusive')
		if self.model_services:
			seen_names: set[str] = set()
			for service in self.model_services:
				if not service.name.strip():
					raise ValueError('model service names must not be empty')
				if service.name in seen_names:
					raise ValueError(f'duplicate model service name: {service.name}')
				seen_names.add(service.name)
				if not service.api_base.strip():
					raise ValueError(f'model service {service.name} api_base must not be empty')
				if not service.api_key.strip():
					raise ValueError(f'model service {service.name} api_key must not be empty')
				if not service.model.strip():
					raise ValueError(f'model service {service.name} model must not be empty')
				if service.response_mode not in {'responses', 'chat-completions'}:
					raise ValueError(f'unsupported model service response mode: {service.name}')
		elif not self.vlm_ports:
			raise ValueError('at least one model service is required')
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
		if self.experiment_mode and self.local_browser:
			raise ValueError('experiment_mode requires one or more CDP URLs, not local_browser')
		if self.experiment_repeat_index < 0:
			raise ValueError('experiment_repeat_index must be non-negative')
		if self.experiment_endpoint_label is not None:
			if not self.experiment_endpoint_label.strip() or len(self.experiment_endpoint_label) > 80:
				raise ValueError('experiment_endpoint_label must be a non-empty label no longer than 80 characters')
		if self.browser_driver is BrowserDriver.PATCHRIGHT and not self.experiment_mode:
			if self.patchright_qualification_report is None:
				raise ValueError('Patchright requires a passing --patchright-qualification-report before a formal CDP run')
			if not patchright_qualification_report_passes(self.patchright_qualification_report):
				raise ValueError('Patchright qualification report is missing or did not pass the experiment gate')
		if self.browser_driver is BrowserDriver.REBROWSER and not self.experiment_mode:
			if self.rebrowser_qualification_report is None:
				raise ValueError('Rebrowser requires a passing --rebrowser-qualification-report before a formal CDP run')
			if not rebrowser_qualification_report_passes(self.rebrowser_qualification_report):
				raise ValueError('Rebrowser qualification report is missing or did not pass the experiment gate')
		self.sec_user_agent = normalize_sec_user_agent(self.sec_user_agent)
		self.thought_language = normalize_thought_language(self.thought_language)


@dataclass(frozen=True, slots=True)
class TaskRunResult:
	status: str
	retire_worker: bool = False
	recover_worker: bool = False
	requeue_task: bool = False
	answer: str | None = None
	error: str | None = None


@dataclass(slots=True)
class _WorkerPoolState:
	"""Coordinate idle workers while another worker may return a task."""

	starting_workers: int
	ready_workers: int = 0
	inflight_tasks: int = 0
	_condition: asyncio.Condition = field(default_factory=asyncio.Condition)

	async def startup_finished(self, *, succeeded: bool) -> None:
		async with self._condition:
			self.starting_workers = max(0, self.starting_workers - 1)
			if succeeded:
				self.ready_workers += 1
			self._condition.notify_all()

	def task_claimed(self) -> None:
		# This is intentionally synchronous: the queue item has just been
		# removed, so increment the in-flight count before yielding control to
		# another worker that may observe an empty queue.
		self.inflight_tasks += 1

	async def task_finished(self) -> None:
		async with self._condition:
			self.inflight_tasks = max(0, self.inflight_tasks - 1)
			self._condition.notify_all()

	async def task_requeued(self) -> None:
		async with self._condition:
			self._condition.notify_all()

	async def wait_for_work(self, queue: asyncio.Queue[CompetitionTask]) -> bool:
		"""Wait until a task is queued or every claimed task is finished."""

		async with self._condition:
			while queue.empty() and self.inflight_tasks > 0:
				await self._condition.wait()
			return not queue.empty()


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
	# The local OpenAI-compatible gateways used by this workspace expose Responses.
	return False


def _build_chat_model(
	config: RunnerConfig,
	*,
	api_base: str | None,
	api_key: str,
	model: str | None = None,
	use_responses_api: bool | None = None,
) -> ChatOpenAI:
	if use_responses_api is None:
		use_responses_api = resolve_responses_api(config.api_mode)
	return ChatOpenAI(
		model=model or config.model,
		api_key=api_key,
		base_url=api_base or None,
		use_responses_api=use_responses_api,
		stream_responses_api=use_responses_api,
		timeout=config.model_timeout_seconds,
		max_retries=0,
		temperature=0.1,
		reasoning_effort=config.reasoning_effort,
		max_completion_tokens=4096,
		default_headers={'User-Agent': 'python-httpx/0.28.1'},
	)


def build_llm(config: RunnerConfig, worker_id: int = 0) -> BaseChatModel:
	if config.model_services:
		services = tuple(config.model_services)
		clients = tuple(
			_build_chat_model(
				config,
				api_base=service.api_base,
				api_key=service.api_key,
				model=service.model,
				use_responses_api=resolve_responses_api(service.response_mode),
			)
			for service in services
		)
		return ModelServiceRouter(
			model=config.model,
			services=services,
			clients=clients,
			model_timeout_seconds=config.model_timeout_seconds,
		)

	if config.vlm_ports:
		base_url = f'http://127.0.0.1:{config.vlm_ports[worker_id % len(config.vlm_ports)]}/v1'
		use_responses_api = resolve_responses_api(config.api_mode)
		if config.api_mode == 'auto':
			use_responses_api = False
		return _build_chat_model(
			config,
			api_base=base_url,
			api_key='not-required',
			use_responses_api=use_responses_api,
		)

	raise ValueError('model_services must be configured for OpenAI-compatible model calls')


def _load_existing_result(writer: TaskArtifactWriter) -> Mapping[str, Any] | None:
	try:
		with writer.result_path.open(encoding='utf-8') as result_file:
			payload = json.load(result_file)
	except (FileNotFoundError, OSError, json.JSONDecodeError):
		return None
	return payload if isinstance(payload, Mapping) else None


def _should_skip(result: Mapping[str, Any] | None, *, rerun_failed: bool) -> bool:
	status = result.get('status') if isinstance(result, Mapping) else None
	if not isinstance(status, str) or status == 'PENDING':
		return False
	if status == 'SUCCESS':
		return valid_completion_receipt(result)
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
	cleanup: Mapping[str, Any] | None = None,
	browser_driver: BrowserDriver | None = None,
	browser_driver_fallback_reason: str | None = None,
	rebrowser_runtime_fix_mode: str | None = None,
	endpoint_label: str | None = None,
	experiment_repeat_index: int | None = None,
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
		'verification': outcome.verification,
		'browser_failure': outcome.browser_failure,
		'model_call_timing_summary': outcome.model_call_timing_summary,
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
	if cleanup is not None:
		payload['cleanup'] = dict(cleanup)
	if browser_driver is not None:
		payload['browser_driver'] = browser_driver.value
	if browser_driver_fallback_reason is not None:
		payload['browser_driver_fallback_reason'] = browser_driver_fallback_reason
	if rebrowser_runtime_fix_mode is not None:
		payload['rebrowser_runtime_fix_mode'] = rebrowser_runtime_fix_mode
	if endpoint_label is not None:
		payload['experiment_endpoint_label'] = endpoint_label
	if experiment_repeat_index is not None:
		payload['experiment_repeat_index'] = experiment_repeat_index
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


def _log_diagnostic_task_failure(
	logger: logging.Logger,
	task: CompetitionTask,
	outcome: AgentRunOutcome,
) -> None:
	"""Emit failure diagnostics in export-friendly, credential-safe log records."""

	if not outcome.status.startswith('FAIL_') or not outcome.error:
		return
	diagnostic = outcome.browser_failure
	diagnostic_suffix = ''
	if isinstance(diagnostic, Mapping):
		category = diagnostic.get('category')
		subtype = diagnostic.get('subtype')
		if isinstance(category, str) and isinstance(subtype, str):
			diagnostic_suffix = f'; browser_failure={category}/{subtype}'
	detail_chunks = _failure_log_detail_chunks(redact_cdp_url(outcome.error))
	logger.error(
		'Task %s/%s failed with status %s%s; error: detail_chunks=%s',
		task.task_idx,
		task.task_id,
		outcome.status,
		diagnostic_suffix,
		len(detail_chunks),
	)
	for chunk_index, detail_chunk in enumerate(detail_chunks, start=1):
		# The competition's error-log export searches individual lines and truncates
		# long ones.  A separate bounded ERROR record keeps every useful detail
		# searchable instead of hiding it after a newline in the heading record.
		logger.error(
			'Task %s/%s error: detail[%s/%s]: %s',
			task.task_idx,
			task.task_id,
			chunk_index,
			len(detail_chunks),
			detail_chunk,
		)


def _failure_log_detail_chunks(error: str) -> list[str]:
	"""Return bounded one-line chunks suitable for a line-oriented log export."""

	compact_error = ' '.join(error.split()) or '<empty error text>'
	if len(compact_error) > _FAILURE_LOG_DETAIL_MAX_CHARS:
		omitted_characters = len(compact_error) - _FAILURE_LOG_DETAIL_MAX_CHARS
		compact_error = (
			f'{compact_error[:_FAILURE_LOG_DETAIL_MAX_CHARS]} '
			f'[detail truncated; {omitted_characters} additional characters omitted]'
		)
	return [
		compact_error[start : start + _FAILURE_LOG_DETAIL_CHUNK_CHARS]
		for start in range(0, len(compact_error), _FAILURE_LOG_DETAIL_CHUNK_CHARS)
	]


def _is_browser_disconnect_error(error: BaseException | str | None) -> bool:
	"""Backward-compatible name for the shared narrow session-close predicate."""

	return is_browser_session_closed_error(error)


def _is_cdp_connection_unavailable_error(error: BaseException | str | None) -> bool:
	"""Recognize connection failures that happen before a task can start."""

	message = str(error).casefold() if error is not None else ''
	return any(marker in message for marker in ('cdp connection unavailable', 'connect_over_cdp'))


async def _run_task(
	*,
	context: BrowserContext | None,
	task: CompetitionTask,
	config: RunnerConfig,
	llm: BaseChatModel,
	logger: logging.Logger,
	browser_driver: BrowserDriver | None = None,
	browser_driver_fallback_reason: str | None = None,
	rebrowser_runtime_fix_mode: str | None = None,
	endpoint_label: str | None = None,
	browser_session: CdpWorkerSession | None = None,
	observer: RunObserver | None = None,
	cancellation: CancellationToken | None = None,
	worker_id: int | None = None,
) -> TaskRunResult:
	observer = observer or NoOpRunObserver()
	cancellation = cancellation or CancellationToken()
	writer = TaskArtifactWriter(config.output_dir, task)
	writer.prepare()
	if not writer.acquire_lock(blocking=False):
		logger.info('Skipping task %s/%s because another worker owns its lock', task.task_idx, task.task_id)
		return TaskRunResult('LOCKED')

	try:
		if cancellation.cancelled:
			outcome = AgentRunOutcome(
				status='FAIL_CANCELLED',
				error=f'cancelled_before_start: {cancellation.reason}',
			)
			writer.write_result(_result_payload(task, outcome, urls=[], model=config.model))
			writer.write_capture()
			return TaskRunResult(outcome.status)
		# Re-read only after acquiring ownership, so two runners cannot both pass
		# the resume check and execute the same formal task.
		existing_result = _load_existing_result(writer)
		existing_status = existing_result.get('status') if isinstance(existing_result, Mapping) else None
		if not isinstance(existing_status, str):
			existing_status = None
		if _should_skip(existing_result, rerun_failed=config.rerun_failed):
			logger.info('Skipping task %s/%s with existing status %s', task.task_idx, task.task_id, existing_status)
			return TaskRunResult(existing_status or 'SKIPPED')

		task_started_at = datetime.now(timezone.utc)
		task_started_monotonic = time.monotonic()
		runtime: BrowserRuntime | None = None
		agent: ProtocolIIIAgent | None = None
		cleanup: dict[str, Any] | None = None
		retire_worker = False
		recover_worker = False
		requeue_task = False
		browser_startup_in_progress = False
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

			browser_request = TaskBrowserRequest(
				website=task.website,
				task_dir=writer.task_dir,
				logger=logger,
				declared_user_agent=config.sec_user_agent if is_sec_task else None,
				task_identity=task.prompt_payload(),
			)
			browser_startup_in_progress = True
			if browser_session is not None:
				runtime = await browser_session.open_task_runtime(
					browser_request,
					deadline_monotonic=task_started_monotonic + config.task_timeout_seconds,
				)
			else:
				if context is None:
					raise RuntimeError('worker has no browser context')
				runtime = BrowserRuntime(
					context,
					writer.task_dir,
					logger,
					declared_user_agent=config.sec_user_agent if is_sec_task else None,
					task_identity=task.prompt_payload(),
				)
				await _await_with_hard_timeout(
					runtime.start(task.website),
					_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
				)
			# Checkpoint genuine browser traffic before model work begins.  A slow or
			# cancelled decision loop must not erase evidence that this task reached
			# its evaluator-provided browser and start URL.
			writer.write_capture(runtime.capture_payload())
			browser_startup_in_progress = False

			async def recover_unstarted_runtime() -> BrowserRuntime:
				nonlocal runtime
				if runtime is not None:
					try:
						cleanup_budget = min(
							1.0,
							_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
						)
						await _await_with_hard_timeout(
							runtime.close(timeout_seconds=cleanup_budget),
							cleanup_budget,
						)
					except Exception as exc:
						logger.warning('Could not clean up the unstarted browser runtime before replacement: %s', exc)
				deadline_monotonic = task_started_monotonic + config.task_timeout_seconds
				if browser_session is not None:
					runtime = await browser_session.replace_unstarted_task_runtime(
						browser_request,
						deadline_monotonic=deadline_monotonic,
					)
					return runtime
				if context is None:
					raise RuntimeError('worker has no browser context')
				runtime = BrowserRuntime(
					context,
					writer.task_dir,
					logger,
					declared_user_agent=config.sec_user_agent if is_sec_task else None,
					task_identity=task.prompt_payload(),
				)
				await _await_with_hard_timeout(
					runtime.start(task.website),
					_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
				)
				return runtime

			agent = ProtocolIIIAgent(
				task=task,
				llm=llm,
				runtime=runtime,
				task_dir=writer.task_dir,
				max_steps=config.max_steps,
				model_timeout_seconds=config.model_timeout_seconds,
				thought_language=config.thought_language,
				structured_prompt_log=config.structured_prompt_log,
				cancellation=cancellation,
				observer=observer,
				worker_id=worker_id,
				task_deadline_monotonic=task_started_monotonic + config.task_timeout_seconds,
				recover_unstarted_runtime=recover_unstarted_runtime,
			)
			outcome = await _await_with_hard_timeout(
				agent.run(),
				_remaining_task_seconds(task_started_monotonic, config.task_timeout_seconds),
			)
			runtime = getattr(agent, 'runtime', runtime)
			if browser_session is not None and (
				_is_browser_disconnect_error(outcome.error)
				or outcome.status == 'FAIL_BROWSER_TASK_PAGE_UNAVAILABLE'
			):
				recover_worker = True
		except BrowserRecoveryDeadlineExceeded:
			outcome = _task_timeout_outcome(agent, config.task_timeout_seconds)
			outcome.error = 'Browser recovery exhausted the task deadline'
			# A task that has not completed browser startup has no observable
			# side-effects yet.  Return it to the shared queue so a healthy CDP
			# worker can take over instead of persisting a terminal timeout.
			if browser_session is not None and browser_startup_in_progress:
				requeue_task = True
				recover_worker = True
			retire_worker = True
		except TimeoutError:
			outcome = _task_timeout_outcome(agent, config.task_timeout_seconds)
		except Exception as exc:
			if browser_session is not None and _is_browser_disconnect_error(exc):
				recover_worker = True
			if (
				browser_session is not None
				and browser_startup_in_progress
				and _is_cdp_connection_unavailable_error(exc)
			):
				requeue_task = True
				retire_worker = True
			logger.exception('Task %s/%s crashed', task.task_idx, task.task_id)
			if browser_startup_in_progress:
				browser_failure = classify_browser_failure(
					exc,
					phase=BrowserFailurePhase.STARTUP,
					session_closed=_is_browser_disconnect_error(exc),
				)
				outcome = AgentRunOutcome(
					status=browser_failure.status,
					error=f'Browser startup failed: {type(exc).__name__}: {exc}',
					browser_failure=browser_failure.payload(recovery_attempted=False),
				)
			else:
				outcome = AgentRunOutcome(
					status='FAIL_RUNTIME',
					error=f'{type(exc).__name__}: {exc}',
				)
		finally:
			if runtime is not None:
				cleanup_started_at = time.monotonic()
				try:
					report = await _await_with_hard_timeout(
						runtime.close(timeout_seconds=TASK_FINALIZATION_GRACE_SECONDS),
						TASK_FINALIZATION_GRACE_SECONDS,
					)
					cleanup = dict(report) if isinstance(report, Mapping) else {}
					cleanup.setdefault('status', 'completed')
				except TimeoutError:
					cleanup = {'status': 'timed_out'}
					with contextlib.suppress(Exception):
						report = runtime.cleanup_diagnostics()
						if isinstance(report, Mapping):
							cleanup.update(dict(report))
					cleanup['status'] = 'timed_out'
					logger.warning(
						'Runtime cleanup exceeded the %g-second finalization window for task %s',
						TASK_FINALIZATION_GRACE_SECONDS,
						task.task_id,
					)
				except Exception as exc:
					if browser_session is not None and _is_browser_disconnect_error(exc):
						recover_worker = True
					cleanup = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
					logger.warning('Runtime cleanup failed for task %s: %s', task.task_id, exc)
				cleanup['grace_seconds'] = TASK_FINALIZATION_GRACE_SECONDS
				cleanup['elapsed_seconds'] = round(time.monotonic() - cleanup_started_at, 3)

		if recover_worker and browser_session is not None and not retire_worker:
			try:
				await browser_session.abandon_interrupted_task()
			except Exception as exc:
				logger.warning('Could not discard the interrupted CDP session: %s', redact_cdp_url(str(exc)))
				recover_worker = False
				retire_worker = True
		if requeue_task:
			logger.warning(
				'CDP startup failed before task %s/%s began; returning it to the shared queue',
				task.task_idx,
				task.task_id,
			)
			return TaskRunResult(
				'REQUEUED',
				retire_worker=retire_worker,
				recover_worker=recover_worker,
				requeue_task=True,
			)

		urls = list(runtime.visited_urls) if runtime is not None else []
		capture = runtime.capture_payload() if runtime is not None else None
		writer.write_capture(capture)
		writer.write_model_call(getattr(agent, 'model_call_timing_payload', None))
		await emit_safely(
			observer,
			RunnerEvent(
				type='task.phase_changed',
				worker_id=worker_id,
				task_id=task.task_id,
				task_idx=task.task_idx,
				payload={'phase': 'finalizing'},
			),
			logger=logger,
		)
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
				cleanup=cleanup,
				browser_driver=browser_driver if config.experiment_mode else None,
				browser_driver_fallback_reason=browser_driver_fallback_reason if config.experiment_mode else None,
				rebrowser_runtime_fix_mode=rebrowser_runtime_fix_mode if config.experiment_mode else None,
				endpoint_label=endpoint_label if config.experiment_mode else None,
				experiment_repeat_index=config.experiment_repeat_index if config.experiment_mode else None,
			)
		)
		_log_diagnostic_task_failure(logger, task, outcome)
		logger.info('Finished task %s/%s with status %s', task.task_idx, task.task_id, outcome.status)
		return TaskRunResult(
			outcome.status,
			retire_worker=retire_worker,
			recover_worker=recover_worker,
			answer=outcome.agent_answer,
			error=redact_cdp_url(outcome.error) if outcome.error else None,
		)
	finally:
		try:
			if isinstance(llm, ModelServiceRouter):
				await llm.clear_task_affinity(task.task_id)
		finally:
			writer.release_lock()


async def _consume_tasks(
	*,
	worker_id: int,
	context: BrowserContext | None,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	llm: BaseChatModel,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
	browser_driver: BrowserDriver | None = None,
	browser_driver_fallback_reason: str | None = None,
	rebrowser_runtime_fix_mode: str | None = None,
	endpoint_label: str | None = None,
	browser: Browser | None = None,
	browser_session: CdpWorkerSession | None = None,
	worker_pool: _WorkerPoolState | None = None,
	observer: RunObserver | None = None,
	cancellation: CancellationToken | None = None,
) -> None:
	observer = observer or NoOpRunObserver()
	cancellation = cancellation or CancellationToken()
	logger = _worker_logger(config.output_dir, worker_id)
	while True:
		if cancellation.cancelled:
			return
		if browser_session is not None and browser_session.recovery_required:
			recovery_deadline = time.monotonic() + TASK_FINALIZATION_GRACE_SECONDS
			try:
				await browser_session.recover_before_next_task(deadline_monotonic=recovery_deadline)
			except Exception as exc:
				logger.error(
					'Browser worker recovery failed within its %g-second window; retiring worker %s: %s',
					TASK_FINALIZATION_GRACE_SECONDS,
					worker_id,
					redact_cdp_url(f'{type(exc).__name__}: {exc}'),
				)
				return
		if cancellation.cancelled:
			return
		try:
			task = queue.get_nowait()
		except asyncio.QueueEmpty:
			if worker_pool is None or not await worker_pool.wait_for_work(queue):
				return
			continue
		if worker_pool is not None:
			worker_pool.task_claimed()
		try:
			await emit_safely(
				observer,
				RunnerEvent(
					type='task.started',
					worker_id=worker_id,
					task_id=task.task_id,
					task_idx=task.task_idx,
					payload={'website_display': task.website.split('?', maxsplit=1)[0].split('#', maxsplit=1)[0]},
				),
				logger=logger,
			)
			is_sec_task = is_sec_url(task.website)
			acquired_sec_slot = False
			task_context: BrowserContext | None = context
			created_experiment_context = False
			try:
				if config.experiment_mode:
					if browser is None:
						raise RuntimeError('experiment_mode requires a connected CDP browser')
					task_context = await browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 900})
					created_experiment_context = True
				if task_context is None and browser_session is None:
					raise RuntimeError('worker has no browser context')
				if is_sec_task:
					await sec_task_semaphore.acquire()
					acquired_sec_slot = True
				task_result = await _run_task(
					context=task_context,
					task=task,
					config=config,
					llm=llm,
					logger=logger,
					browser_driver=browser_driver,
					browser_driver_fallback_reason=browser_driver_fallback_reason,
					rebrowser_runtime_fix_mode=rebrowser_runtime_fix_mode,
					endpoint_label=endpoint_label,
					browser_session=browser_session,
					observer=observer,
					cancellation=cancellation,
					worker_id=worker_id,
				)
				if task_result.requeue_task:
					queue.put_nowait(task)
					if worker_pool is not None:
						await worker_pool.task_requeued()
				else:
					statuses[task.task_id] = task_result.status
					await emit_safely(
						observer,
						RunnerEvent(
							type='task.finished',
							worker_id=worker_id,
							task_id=task.task_id,
							task_idx=task.task_idx,
							payload={
								'domain_status': task_result.status,
								**({'answer': task_result.answer} if task_result.answer else {}),
								**({'error': task_result.error} if task_result.error else {}),
							},
						),
						logger=logger,
					)
				if task_result.retire_worker:
					logger.error('Browser session became unusable; retiring worker %s', worker_id)
					return
				if task_result.recover_worker:
					logger.warning('Browser session interrupted; recovering worker %s before the next task', worker_id)
			except Exception as exc:
				# Artifact I/O and other runner-level failures must not abandon the
				# remainder of this worker's one-shot task shard.
				logger.exception('Runner failed while finalizing task %s/%s', task.task_idx, task.task_id)
				failure = AgentRunOutcome(status='FAIL_RUNNER', error=f'{type(exc).__name__}: {exc}')
				statuses[task.task_id] = failure.status
				_log_diagnostic_task_failure(logger, task, failure)
				try:
					writer = TaskArtifactWriter(config.output_dir, task)
					writer.write_result(_result_payload(task, failure, urls=[], model=config.model))
					writer.write_capture()
				except Exception:
					logger.exception('Could not persist runner failure for task %s/%s', task.task_idx, task.task_id)
				await emit_safely(
					observer,
					RunnerEvent(
						type='task.finished',
						worker_id=worker_id,
						task_id=task.task_id,
						task_idx=task.task_idx,
						payload={'domain_status': failure.status},
					),
					logger=logger,
				)
			finally:
				if acquired_sec_slot:
					sec_task_semaphore.release()
				if created_experiment_context and task_context is not None:
					try:
						await task_context.close()
					except Exception as exc:
						logger.warning('Could not close isolated experiment context: %s', exc)
		finally:
			queue.task_done()
			if worker_pool is not None:
				await worker_pool.task_finished()


async def _cdp_worker(
	*,
	worker_id: int,
	cdp_url: str,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
	llm: BaseChatModel,
	worker_pool: _WorkerPoolState | None = None,
	observer: RunObserver | None = None,
	cancellation: CancellationToken | None = None,
) -> None:
	observer = observer or NoOpRunObserver()
	cancellation = cancellation or CancellationToken()
	logger = _worker_logger(config.output_dir, worker_id)
	logger.info('Connecting to CDP browser %s', redact_cdp_url(cdp_url))
	connection = None
	browser_session: CdpWorkerSession | None = None
	startup_reported = False
	startup_succeeded = False
	try:
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'STARTING'}),
			logger=logger,
		)
		headers = (
			config.cdp_headers_provider(cdp_url)
			if config.cdp_headers_provider is not None
			else cdp_headers_for_url(cdp_url)
		)
		connector = BrowserConnector()
		connection = await connector.connect(
			config.browser_driver,
			cdp_url,
			headers=dict(headers) or None,
		)
		browser = connection.browser
		if connection.fallback_reason is not None:
			logger.warning(
				'Patchright could not attach before task start; using Playwright fallback: %s', connection.fallback_reason
			)
		context = None
		if not config.experiment_mode:
			browser_session = await CdpWorkerSession.from_connection(
				cdp_url=cdp_url,
				driver=connection.driver,
				headers=dict(headers) or None,
				logger=logger,
				connector=connector,
				connection=connection,
			)
		startup_succeeded = True
		if worker_pool is not None:
			await worker_pool.startup_finished(succeeded=startup_succeeded)
			startup_reported = True
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'READY'}),
			logger=logger,
		)
		await _consume_tasks(
			worker_id=worker_id,
			context=context,
			browser=browser,
			queue=queue,
			config=config,
			llm=llm,
			statuses=statuses,
			sec_task_semaphore=sec_task_semaphore,
			browser_driver=connection.driver,
			browser_driver_fallback_reason=connection.fallback_reason,
			rebrowser_runtime_fix_mode=connection.rebrowser_runtime_fix_mode,
			endpoint_label=config.experiment_endpoint_label or f'cdp-{worker_id}',
			browser_session=browser_session,
			worker_pool=worker_pool,
			observer=observer,
			cancellation=cancellation,
		)
	except Exception as exc:
		# Playwright connection errors may repeat the endpoint verbatim.  Avoid
		# traceback logging here so evaluator access tokens never reach artifacts.
		logger.error('CDP worker failed for %s: %s', redact_cdp_url(cdp_url), redact_cdp_url(str(exc)))
	finally:
		if worker_pool is not None and not startup_reported:
			with contextlib.suppress(Exception):
				await worker_pool.startup_finished(succeeded=startup_succeeded)
		if browser_session is not None:
			try:
				await browser_session.close()
			except Exception as exc:
				logger.warning('Could not close CDP worker session: %s', redact_cdp_url(str(exc)))
		elif connection is not None:
			try:
				await connection.close()
			except Exception as exc:
				logger.warning('Could not close CDP connection: %s', redact_cdp_url(str(exc)))
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'RETIRED'}),
			logger=logger,
		)


async def _local_worker(
	*,
	worker_id: int,
	browser: Browser,
	queue: asyncio.Queue[CompetitionTask],
	config: RunnerConfig,
	statuses: dict[str, str],
	sec_task_semaphore: asyncio.Semaphore,
	llm: BaseChatModel,
	worker_pool: _WorkerPoolState | None = None,
	observer: RunObserver | None = None,
	cancellation: CancellationToken | None = None,
) -> None:
	"""Run one isolated local browser context for each concurrent worker."""

	observer = observer or NoOpRunObserver()
	cancellation = cancellation or CancellationToken()
	logger = _worker_logger(config.output_dir, worker_id)
	context: BrowserContext | None = None
	try:
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'STARTING'}),
			logger=logger,
		)
		context = await browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 900})
		if worker_pool is not None:
			await worker_pool.startup_finished(succeeded=True)
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'READY'}),
			logger=logger,
		)
		await _consume_tasks(
			worker_id=worker_id,
			context=context,
			browser=browser,
			queue=queue,
			config=config,
			llm=llm,
			statuses=statuses,
			sec_task_semaphore=sec_task_semaphore,
			worker_pool=worker_pool,
			observer=observer,
			cancellation=cancellation,
		)
	except Exception:
		logger.exception('Local browser worker failed')
		if worker_pool is not None and context is None:
			with contextlib.suppress(Exception):
				await worker_pool.startup_finished(succeeded=False)
	finally:
		if context is not None:
			try:
				await context.close()
			except Exception as exc:
				logger.warning('Could not close local browser context: %s', exc)
		await emit_safely(
			observer,
			RunnerEvent(type='worker.state_changed', worker_id=worker_id, payload={'state': 'RETIRED'}),
			logger=logger,
		)


def _select_tasks(tasks: list[CompetitionTask], config: RunnerConfig) -> list[CompetitionTask]:
	selected = tasks
	if config.task_indices is not None:
		selected = [task for task in selected if task.task_idx in config.task_indices]
	if config.limit is not None:
		selected = selected[: config.limit]
	return selected


async def run(
	config: RunnerConfig,
	*,
	observer: RunObserver | None = None,
	cancellation: CancellationToken | None = None,
) -> dict[str, Any]:
	config.validate()
	tasks = _select_tasks(load_tasks(config.input_path), config)
	if config.project_url is not None:
		tasks = [task.model_copy(update={'website': config.project_url}) for task in tasks]
	config.output_dir.mkdir(parents=True, exist_ok=True)
	if not tasks:
		raise ValueError('no tasks selected')

	observer = observer or NoOpRunObserver()
	cancellation = cancellation or CancellationToken()
	queue: asyncio.Queue[CompetitionTask] = asyncio.Queue()
	for task in tasks:
		queue.put_nowait(task)
	statuses: dict[str, str] = {}
	sec_task_semaphore = asyncio.Semaphore(1)
	worker_count = min(config.max_concurrency, len(tasks))
	logger = logging.getLogger('webretriever.runner')
	await emit_safely(
		observer,
		RunnerEvent(type='run.started', payload={'workers_planned': worker_count}),
		logger=logger,
	)
	if cancellation.cancelled:
		while not queue.empty():
			task = queue.get_nowait()
			writer = TaskArtifactWriter(config.output_dir, task)
			writer.prepare()
			outcome = AgentRunOutcome(
				status='FAIL_CANCELLED',
				error=f'cancelled_before_start: {cancellation.reason}',
			)
			writer.write_result(_result_payload(task, outcome, urls=[], model=config.model))
			writer.write_capture()
			statuses[task.task_id] = outcome.status
			queue.task_done()
			await emit_safely(
				observer,
				RunnerEvent(
					type='task.finished',
					task_id=task.task_id,
					task_idx=task.task_idx,
					payload={'domain_status': outcome.status, 'cancelled_before_start': True},
				),
				logger=logger,
			)
		counts: dict[str, int] = {}
		for status in statuses.values():
			counts[status] = counts.get(status, 0) + 1
		summary = {
			'created_at': datetime.now(timezone.utc).isoformat(),
			'input': str(config.input_path),
			'total_selected': len(tasks),
			'max_concurrency': config.max_concurrency,
			'workers_started': 0,
			'workers_ready': 0,
			'counts': counts,
			'statuses': statuses,
		}
		atomic_write_json(config.output_dir / 'logs' / 'summary.json', summary)
		await emit_safely(
			observer,
			RunnerEvent(type='run.cancelled', payload={'completed': 0, 'aborted': len(statuses)}),
			logger=logger,
		)
		return summary
	# Model-service clients and their state are shared by every worker in this
	# run. Local VLM endpoints deliberately retain their worker-specific routing.
	shared_model_router = build_llm(config) if config.model_services else None
	if config.local_browser:
		worker_pool = _WorkerPoolState(worker_count)
		async with async_playwright() as playwright:
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
						llm=shared_model_router
						if shared_model_router is not None
						else build_llm(config, worker_id),
						worker_pool=worker_pool,
						observer=observer,
						cancellation=cancellation,
					)
						for worker_id in range(worker_count)
					)
				)
			finally:
				await browser.close()
	else:
		worker_urls = config.cdp_urls[:worker_count]
		worker_count = len(worker_urls)
		worker_pool = _WorkerPoolState(worker_count)
		await asyncio.gather(
			*(
				_cdp_worker(
					worker_id=worker_id,
					cdp_url=cdp_url,
					queue=queue,
					config=config,
					statuses=statuses,
					sec_task_semaphore=sec_task_semaphore,
					llm=shared_model_router
					if shared_model_router is not None
					else build_llm(config, worker_id),
					worker_pool=worker_pool,
					observer=observer,
					cancellation=cancellation,
				)
				for worker_id, cdp_url in enumerate(worker_urls)
			)
		)

	# A connection failure must still produce one diagnostic result per task.
	while not queue.empty():
		task = queue.get_nowait()
		writer = TaskArtifactWriter(config.output_dir, task)
		writer.prepare()
		if cancellation.cancelled:
			failure = AgentRunOutcome(
				status='FAIL_CANCELLED',
				error=f'cancelled_before_start: {cancellation.reason}',
			)
		else:
			browser_failure = classify_browser_failure(
				'No CDP worker was available for this task',
				phase=BrowserFailurePhase.STARTUP,
			)
			failure = AgentRunOutcome(
				status=browser_failure.status,
				error='No CDP worker was available for this task',
				browser_failure=browser_failure.payload(recovery_attempted=False),
			)
		writer.write_result(_result_payload(task, failure, urls=[], model=config.model))
		writer.write_capture()
		_log_diagnostic_task_failure(_worker_logger(config.output_dir, -1), task, failure)
		statuses[task.task_id] = failure.status
		queue.task_done()
		await emit_safely(
			observer,
			RunnerEvent(
				type='task.finished',
				task_id=task.task_id,
				task_idx=task.task_idx,
				payload={'domain_status': failure.status, 'cancelled_before_start': cancellation.cancelled},
			),
			logger=logger,
		)

	counts: dict[str, int] = {}
	for status in statuses.values():
		counts[status] = counts.get(status, 0) + 1
	summary = {
		'created_at': datetime.now(timezone.utc).isoformat(),
		'input': str(config.input_path),
		'total_selected': len(tasks),
		'max_concurrency': config.max_concurrency,
		'workers_started': worker_count,
		'workers_ready': worker_pool.ready_workers,
		'counts': counts,
		'statuses': statuses,
	}
	atomic_write_json(config.output_dir / 'logs' / 'summary.json', summary)
	terminal_type = 'run.cancelled' if cancellation.cancelled else 'run.completed'
	await emit_safely(
		observer,
		RunnerEvent(
			type=terminal_type,
			payload={
				'completed': sum(status != 'FAIL_CANCELLED' for status in statuses.values()),
				'aborted': sum(status == 'FAIL_CANCELLED' for status in statuses.values()),
			},
		),
		logger=logger,
	)
	return summary


def _experiment_records_from_artifacts(
	*,
	output_dir: Path,
	tasks: list[CompetitionTask],
	driver: BrowserDriver,
	endpoint_label: str,
	repeat_index: int,
) -> list[ExperimentRecord]:
	"""Read one isolated run's task artifacts without retaining CDP credentials."""

	records: list[ExperimentRecord] = []
	for task in tasks:
		writer = TaskArtifactWriter(output_dir, task)
		try:
			with writer.result_path.open(encoding='utf-8') as result_file:
				payload = json.load(result_file)
		except (FileNotFoundError, OSError, json.JSONDecodeError):
			payload = {}
		verification = payload.get('verification') if isinstance(payload, dict) else None
		artifact_complete = _has_complete_experiment_evidence(
			payload=payload,
			verification=verification,
			task=task,
			driver=driver,
			endpoint_label=endpoint_label,
			repeat_index=repeat_index,
		)
		challenge_episodes = verification.get('challenge_episodes', 0) if isinstance(verification, dict) else 0
		if type(challenge_episodes) is not int or challenge_episodes < 0:
			challenge_episodes = 0
		status = payload.get('status', 'FAIL_ARTIFACT') if isinstance(payload, dict) else 'FAIL_ARTIFACT'
		answer = payload.get('agent_answer', '') if isinstance(payload, dict) else ''
		fallback_reason = payload.get('browser_driver_fallback_reason') if isinstance(payload, dict) else None
		runtime_fix_mode = payload.get('rebrowser_runtime_fix_mode') if isinstance(payload, dict) else None
		records.append(
			ExperimentRecord(
				driver=driver,
				endpoint_label=endpoint_label,
				task_idx=task.task_idx,
				task_id=task.task_id,
				repeat_index=repeat_index,
				challenge_episodes=challenge_episodes,
				status=str(status),
				agent_answer=str(answer),
				fallback_reason=str(fallback_reason) if fallback_reason else None,
				runtime_fix_mode=str(runtime_fix_mode) if runtime_fix_mode else None,
				artifact_complete=artifact_complete,
			)
		)
	return records


def _has_complete_experiment_evidence(
	*,
	payload: object,
	verification: object,
	task: CompetitionTask,
	driver: BrowserDriver,
	endpoint_label: str,
	repeat_index: int,
) -> bool:
	"""Require a parseable task result and its bounded verification observation.

	A missing or malformed artifact must not turn into a zero-episode observation:
	otherwise a failed Patchright run could falsely satisfy the reduction gate.
	"""
	if not isinstance(payload, dict) or not isinstance(verification, dict):
		return False
	if (
		payload.get('task_idx') != task.task_idx
		or payload.get('task_id') != task.task_id
		or payload.get('browser_driver') != driver.value
		or payload.get('experiment_endpoint_label') != endpoint_label
		or payload.get('experiment_repeat_index') != repeat_index
	):
		return False
	if driver is BrowserDriver.REBROWSER and payload.get('rebrowser_runtime_fix_mode') not in {
		'addBinding',
		'alwaysIsolated',
		'enableDisable',
		'0',
	}:
		return False
	challenge_episodes = verification.get('challenge_episodes')
	click_count = verification.get('click_count')
	wait_count = verification.get('wait_count')
	state = verification.get('state')
	return (
		type(challenge_episodes) is int
		and challenge_episodes >= 0
		and type(click_count) is int
		and click_count >= 0
		and type(wait_count) is int
		and wait_count >= 0
		and isinstance(state, str)
		and state in {item.value for item in VerificationState}
	)


async def run_patchright_experiment(config: RunnerConfig) -> ExperimentSummary:
	"""Run the agreed AB/BA Patchright matrix against isolated local CDP contexts.

	For every configured endpoint, the selected tasks run twice: Playwright then
	Patchright in round zero, Patchright then Playwright in round one.  The normal
	runner remains responsible for screenshots, browser actions, and task output;
	this function merely schedules isolated runs and writes the decision report.
	"""

	experiment_input = replace(config, experiment_mode=True)
	experiment_input.validate()
	if experiment_input.local_browser:
		raise ValueError('Patchright experiments require CDP URLs')
	if len(experiment_input.cdp_urls) != 3:
		raise ValueError('Patchright experiments require exactly three CDP URLs')
	if experiment_input.limit is not None:
		raise ValueError('Patchright experiments must not use --limit')
	if experiment_input.task_indices is not None and experiment_input.task_indices != DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES:
		raise ValueError(
			f'Patchright experiments require exactly task indices {sorted(DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES)}'
		)
	base_config = replace(
		experiment_input,
		task_indices=DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES,
		max_concurrency=1,
		experiment_mode=True,
		rerun_failed=False,
	)
	tasks = _select_tasks(load_tasks(base_config.input_path), base_config)
	if {task.task_idx for task in tasks} != DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES:
		raise ValueError('Patchright experiment task file is missing one or more required task indices')

	records: list[ExperimentRecord] = []
	for endpoint_index, cdp_url in enumerate(base_config.cdp_urls):
		endpoint_label = f'cdp-{endpoint_index}'
		for repeat_index in range(2):
			drivers = (
				(BrowserDriver.PLAYWRIGHT, BrowserDriver.PATCHRIGHT)
				if repeat_index == 0
				else (BrowserDriver.PATCHRIGHT, BrowserDriver.PLAYWRIGHT)
			)
			for driver in drivers:
				run_output_dir = (
					base_config.output_dir / 'experiment_runs' / endpoint_label / f'round_{repeat_index}' / driver.value
				)
				run_config = replace(
					base_config,
					output_dir=run_output_dir,
					cdp_urls=[cdp_url],
					browser_driver=driver,
					experiment_repeat_index=repeat_index,
					experiment_endpoint_label=endpoint_label,
				)
				await run(run_config)
				records.extend(
					_experiment_records_from_artifacts(
						output_dir=run_output_dir,
						tasks=tasks,
						driver=driver,
						endpoint_label=endpoint_label,
						repeat_index=repeat_index,
					)
				)

	return write_experiment_summary(base_config.output_dir / 'experiment_summary.json', records)


async def run_rebrowser_experiment(config: RunnerConfig) -> ExperimentSummary:
	"""Run the agreed one-endpoint, one-round Playwright/Rebrowser comparison.

	The execution stays serial so the temporary, version-locked Rebrowser driver
	patch is restored before the baseline is ever reused.  Each driver receives
	a fresh task output directory while both attach to the same evaluator-owned
	CDP endpoint.
	"""

	experiment_input = replace(config, experiment_mode=True)
	experiment_input.validate()
	if experiment_input.local_browser:
		raise ValueError('Rebrowser experiments require a CDP URL')
	if len(experiment_input.cdp_urls) != 1:
		raise ValueError('Rebrowser experiments require exactly one CDP URL')
	if experiment_input.limit is not None:
		raise ValueError('Rebrowser experiments must not use --limit')
	if experiment_input.task_indices is not None and experiment_input.task_indices != DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES:
		raise ValueError(
			f'Rebrowser experiments require exactly task indices {sorted(DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES)}'
		)
	base_config = replace(
		experiment_input,
		task_indices=DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES,
		max_concurrency=1,
		experiment_mode=True,
		rerun_failed=False,
	)
	tasks = _select_tasks(load_tasks(base_config.input_path), base_config)
	if {task.task_idx for task in tasks} != DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES:
		raise ValueError('Rebrowser experiment task file is missing one or more required task indices')

	endpoint_label = 'cdp-0'
	cdp_url = base_config.cdp_urls[0]
	records: list[ExperimentRecord] = []
	for driver in (BrowserDriver.PLAYWRIGHT, BrowserDriver.REBROWSER):
		run_output_dir = base_config.output_dir / 'experiment_runs' / endpoint_label / 'round_0' / driver.value
		run_config = replace(
			base_config,
			output_dir=run_output_dir,
			cdp_urls=[cdp_url],
			browser_driver=driver,
			experiment_repeat_index=0,
			experiment_endpoint_label=endpoint_label,
		)
		await run(run_config)
		records.extend(
			_experiment_records_from_artifacts(
				output_dir=run_output_dir,
				tasks=tasks,
				driver=driver,
				endpoint_label=endpoint_label,
				repeat_index=0,
			)
		)

	return write_experiment_summary(
		base_config.output_dir / 'experiment_summary.json',
		records,
		candidate_driver=BrowserDriver.REBROWSER,
	)


__all__ = [
	'DEFAULT_MAX_CONCURRENCY',
	'DEFAULT_PATCHRIGHT_EXPERIMENT_TASK_INDICES',
	'DEFAULT_REBROWSER_EXPERIMENT_TASK_INDICES',
	'MAX_CONCURRENCY',
	'RunnerConfig',
	'build_llm',
	'normalize_sec_user_agent',
	'resolve_responses_api',
	'run',
	'run_patchright_experiment',
	'run_rebrowser_experiment',
]
