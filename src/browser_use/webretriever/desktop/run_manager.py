"""Durable asynchronous lifecycle for a single active desktop run."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from browser_use.webretriever.desktop.artifact_index import ArtifactIndex
from browser_use.webretriever.desktop.contracts import (
	Artifact,
	ProjectHistoryItem,
	ProjectSummary,
	RunAccepted,
	RunEvent,
	RunEventDraft,
	RunSnapshot,
	RunSpec,
)
from browser_use.webretriever.desktop.store import (
	SqliteControlStore,
	StoredRun,
)
from browser_use.webretriever.run_control import CancellationToken


_ACTIVE_STATUSES = frozenset({"STARTING", "RUNNING", "CANCELLING"})
_TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"})


class DesktopRunner(Protocol):
	"""The fakeable boundary between the control plane and RunnerAdapter."""

	async def run(
		self,
		spec: RunSpec,
		emit: Callable[[RunEventDraft], Awaitable[None]],
		*,
		cancellation: CancellationToken,
		output_dir: Path,
	) -> dict[str, Any]: ...


class ActiveRunExistsError(RuntimeError):
	"""Raised when a distinct request would violate the one-active-run contract."""

	def __init__(self, run_id: str) -> None:
		super().__init__(f"run {run_id} is still active")
		self.run_id = run_id


class IdempotencyKeyReusedError(ValueError):
	"""Raised when one client key names two semantically different run requests."""


class RunNotFoundError(KeyError):
	"""Raised when the requested durable run record does not exist."""


class ProjectNotFoundError(KeyError):
	"""Raised when the requested durable project record does not exist."""


class ActiveProjectRunError(RuntimeError):
	"""Raised when deleting a project would race with its active task."""

	def __init__(self, run_id: str) -> None:
		super().__init__(f"project run {run_id} is still active")
		self.run_id = run_id


class ProjectPathUnsafeError(ValueError):
	"""Raised when a project record points outside sidecar-managed storage."""


class ProjectDeletionPermissionError(PermissionError):
	"""Raised when a deletion cannot access a local project resource."""

	def __init__(self, stage: str) -> None:
		super().__init__(stage)
		self.stage = stage


class EventsExpiredError(ValueError):
	"""Raised when an SSE replay cursor predates the retained journal window."""

	def __init__(self, *, minimum_event_id: int) -> None:
		super().__init__(f"events before {minimum_event_id} are no longer retained")
		self.minimum_event_id = minimum_event_id


@dataclass(slots=True)
class _LiveRun:
	record: StoredRun
	cancellation: CancellationToken
	task: asyncio.Task[None] | None = None


class RunManager:
	"""Persist runs/events before publication and coordinate graceful cancellation."""

	def __init__(
		self,
		*,
		runner: DesktopRunner,
		database_path: Path | str | None = None,
		state_dir: Path | str | None = None,
		event_retention: int = 10_000,
	) -> None:
		self._runner = runner
		database = database_path or ":memory:"
		self._store = SqliteControlStore(database, event_retention=event_retention)
		self._state_dir = Path(state_dir).expanduser().resolve() if state_dir is not None else _database_state_dir(database)
		self._lock = asyncio.Lock()
		self._live: dict[str, _LiveRun] = {}
		self._subscribers: defaultdict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
		self._draining = False
		# A previous process cannot still own a Python task.  Persist its failure
		# before this manager accepts any work, as required by the no-replay rule.
		self._store.reconcile_interrupted()

	@property
	def draining(self) -> bool:
		return self._draining

	async def create_run(self, *, spec: RunSpec, idempotency_key: str) -> RunAccepted:
		"""Durably accept one asynchronous run, or return its idempotent original."""

		spec_payload = spec.model_dump(mode="json")
		spec_digest = json.dumps(spec_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		legacy_spec_payload = {key: value for key, value in spec_payload.items() if key != "project_id"}
		legacy_spec_digest = json.dumps(legacy_spec_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		async with self._lock:
			if self._draining:
				raise SidecarDrainingError
			previous = self._store.get_idempotency(idempotency_key)
			if previous is not None:
				legacy_match = spec.project_id is None and previous.project_id is None and previous.spec_digest == legacy_spec_digest
				if previous.spec_digest != spec_digest and not legacy_match:
					raise IdempotencyKeyReusedError("idempotency key was reused with a different RunSpec")
				return _accepted(previous)
			active = self._store.active_run()
			if active is not None:
				raise ActiveRunExistsError(active.run_id)
			if spec.project_id is not None:
				if spec.project_url is None:
					raise ValueError("project_id requires project_url")
				self._store.ensure_project(spec.project_id, spec.project_url)

			run_id = str(uuid4())
			created_at = datetime.now(timezone.utc)
			output_dir = str(Path(spec.output_root) / f"{created_at:%Y%m%dT%H%M%SZ}_{run_id[:8]}")
			record = StoredRun(
				run_id=run_id,
				idempotency_key=idempotency_key,
				project_id=spec.project_id,
				spec=spec,
				spec_digest=spec_digest,
				status="STARTING",
				created_at=created_at,
				started_at=None,
				finished_at=None,
				output_dir=output_dir,
				summary=None,
				error=None,
			)
			accepted_event = self._store.create_run_with_event(
				record, RunEventDraft(type="run.accepted", level="info", payload={"output_dir": output_dir})
			)
			live = _LiveRun(record=record, cancellation=CancellationToken())
			self._live[run_id] = live
			await self._publish(run_id, accepted_event)
			live.task = asyncio.create_task(self._execute(live), name=f"desktop-run-{run_id}")
			return _accepted(record)

	async def get_run(self, run_id: str) -> RunSnapshot:
		record = self._store.get_run(run_id)
		if record is None:
			raise RunNotFoundError(run_id)
		return record.snapshot(last_event_id=self._store.last_event_id(run_id))

	async def list_runs(self, *, cursor: str | None, limit: int) -> tuple[list[RunSnapshot], str | None]:
		records, next_cursor = self._store.list_runs(cursor=cursor, limit=limit)
		return ([record.snapshot(last_event_id=self._store.last_event_id(record.run_id)) for record in records], next_cursor)

	async def register_project(self, project_id: str, website_url: str) -> ProjectSummary:
		return self._store.ensure_project(project_id, website_url).summary()

	async def list_projects(self, *, limit: int = 100) -> list[ProjectSummary]:
		return [project.summary() for project in self._store.list_projects(limit=limit)]

	async def list_project_history(
		self,
		project_id: str,
		*,
		cursor: str | None,
		limit: int,
	) -> tuple[list[ProjectHistoryItem], str | None]:
		project = self._store.get_project(project_id)
		if project is None:
			raise ProjectNotFoundError(project_id)
		records, next_cursor = self._store.list_project_runs(
			project_id=project_id,
			website_url=project.website_url,
			cursor=cursor,
			limit=limit,
		)
		items = [
			ProjectHistoryItem(
				**record.snapshot(last_event_id=self._store.last_event_id(record.run_id)).model_dump(),
				instruction=_instruction_from_task_file(record.spec.input_path),
				events=self._store.events_after(record.run_id, after=0),
			)
			for record in records
		]
		return items, next_cursor

	async def delete_project(self, project_id: str, *, state_dir: Path | str | None = None) -> None:
		"""Remove one project only after validating and staging all owned files."""

		async with self._lock:
			project = self._store.get_project(project_id)
			if project is None:
				raise ProjectNotFoundError(project_id)
			active = self._store.active_project_run(project_id=project_id, website_url=project.website_url)
			if active is not None:
				raise ActiveProjectRunError(active.run_id)
			records, _ = self._store.list_project_runs(
				project_id=project_id,
				website_url=project.website_url,
				cursor=None,
				limit=10_000,
			)
			try:
				storage_root = Path(state_dir).expanduser().resolve() if state_dir is not None else self._state_dir
				paths = _project_storage_paths(records, storage_root)
				trash_root = storage_root / ".project-delete" / uuid4().hex
			except PermissionError as exc:
				raise ProjectDeletionPermissionError("prepare_storage") from exc
			moved: list[tuple[Path, Path]] = []

			def restore_moved_files() -> None:
				for original, staged in reversed(moved):
					if _path_exists(staged):
						original.parent.mkdir(parents=True, exist_ok=True)
						shutil.move(str(staged), str(original))

			try:
				for index, original in enumerate(paths):
					if not _path_exists(original):
						continue
					staged = trash_root / str(index)
					staged.parent.mkdir(parents=True, exist_ok=True)
					shutil.move(str(original), str(staged))
					moved.append((original, staged))
			except PermissionError as exc:
				try:
					restore_moved_files()
				except PermissionError as rollback_error:
					raise ProjectDeletionPermissionError("rollback") from rollback_error
				raise ProjectDeletionPermissionError("stage_files") from exc
			except BaseException:
				restore_moved_files()
				raise
			try:
				self._store.delete_project_records(project_id, (record.run_id for record in records))
			except PermissionError as exc:
				try:
					restore_moved_files()
				except PermissionError as rollback_error:
					raise ProjectDeletionPermissionError("rollback") from rollback_error
				raise ProjectDeletionPermissionError("delete_records") from exc
			except BaseException:
				restore_moved_files()
				raise
			finally:
				if trash_root.exists():
					shutil.rmtree(trash_root, ignore_errors=True)
					try:
						trash_root.parent.rmdir()
					except OSError:
						pass
			for record in records:
				self._live.pop(record.run_id, None)

	async def list_artifacts(self, run_id: str, *, cursor: str | None, limit: int) -> tuple[list[Artifact], str | None]:
		record = self._store.get_run(run_id)
		if record is None:
			raise RunNotFoundError(run_id)
		return ArtifactIndex(record.output_dir).page(cursor=cursor, limit=limit)

	async def resolve_artifact(self, run_id: str, artifact_id: str) -> Path | None:
		record = self._store.get_run(run_id)
		if record is None:
			raise RunNotFoundError(run_id)
		return ArtifactIndex(record.output_dir).resolve(artifact_id)

	async def wait_for_terminal(self, run_id: str) -> RunSnapshot:
		"""Wait only for this process's background run; historical runs are already final."""

		live = self._live.get(run_id)
		if live is not None and live.task is not None:
			await live.task
		return await self.get_run(run_id)

	async def events_after(self, run_id: str, *, after: int) -> list[RunEvent]:
		if after < 0:
			raise ValueError("after must not be negative")
		if self._store.get_run(run_id) is None:
			raise RunNotFoundError(run_id)
		minimum_event_id = self._store.minimum_event_id(run_id)
		if minimum_event_id is not None and after < minimum_event_id - 1:
			raise EventsExpiredError(minimum_event_id=minimum_event_id)
		return self._store.events_after(run_id, after=after)

	async def request_cancel(self, run_id: str, *, reason: str | None = None) -> tuple[RunSnapshot, bool]:
		"""Request cancellation once; terminal snapshots remain immutable."""

		async with self._lock:
			record = self._store.get_run(run_id)
			if record is None:
				raise RunNotFoundError(run_id)
			if record.status in _TERMINAL_STATUSES:
				return record.snapshot(last_event_id=self._store.last_event_id(run_id)), False
			live = self._live.get(run_id)
			if live is None:
				raise RuntimeError("an active run must be owned by this sidecar")
			applied = live.cancellation.cancel(reason)
			if applied:
				event = self._store.transition_with_event(
					run_id,
					status="CANCELLING",
					draft=RunEventDraft(
						type="run.cancel_requested",
						level="info",
						payload={"requested_at": datetime.now(timezone.utc).isoformat(), "reason": live.cancellation.reason},
					),
				)
				await self._publish(run_id, event)
			return await self.get_run(run_id), applied

	async def request_shutdown(self) -> None:
		"""Enter draining mode and cooperatively cancel the one active run."""

		self._draining = True
		active = self._store.active_run()
		if active is not None:
			await self.request_cancel(active.run_id, reason="sidecar shutdown requested")

	async def wait_for_idle(self) -> None:
		"""Wait for local background work so the sidecar can close after draining."""

		tasks = tuple(live.task for live in self._live.values() if live.task is not None)
		if tasks:
			await asyncio.gather(*tasks, return_exceptions=True)

	def subscribe(self, run_id: str) -> asyncio.Queue[RunEvent]:
		"""Register a bounded live SSE queue before reading journal replay."""

		if self._store.get_run(run_id) is None:
			raise RunNotFoundError(run_id)
		queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=256)
		self._subscribers[run_id].add(queue)
		return queue

	def unsubscribe(self, run_id: str, queue: asyncio.Queue[RunEvent]) -> None:
		subscribers = self._subscribers.get(run_id)
		if subscribers is None:
			return
		subscribers.discard(queue)
		if not subscribers:
			self._subscribers.pop(run_id, None)

	async def stream_events(self, run_id: str, *, after: int) -> AsyncIterator[RunEvent]:
		"""Replay then stream events; clients can safely deduplicate by event_id."""

		queue = self.subscribe(run_id)
		last_id = after
		try:
			for event in await self.events_after(run_id, after=after):
				last_id = event.event_id
				yield event
			while (await self.get_run(run_id)).status not in _TERMINAL_STATUSES:
				event = await queue.get()
				if event.event_id > last_id:
					last_id = event.event_id
					yield event
			for event in await self.events_after(run_id, after=last_id):
				yield event
		finally:
			self.unsubscribe(run_id, queue)

	async def _execute(self, live: _LiveRun) -> None:
		started_at = datetime.now(timezone.utc)
		started_event = self._store.transition_with_event(
			live.record.run_id,
			status="RUNNING",
			started_at=started_at,
			draft=RunEventDraft(type="run.started", level="info", payload={"started_at": started_at.isoformat()}),
		)
		await self._publish(live.record.run_id, started_event)
		try:
			summary = await self._invoke_runner(live)
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			finished_at = datetime.now(timezone.utc)
			error = {"code": "runner_failed", "category": "runner", "message": type(exc).__name__, "retryable": False}
			event = self._store.transition_with_event(
				live.record.run_id,
				status="FAILED",
				finished_at=finished_at,
				error=error,
				draft=RunEventDraft(type="run.failed", level="error", payload={"error": error}),
			)
			await self._publish(live.record.run_id, event)
			return

		finished_at = datetime.now(timezone.utc)
		if live.cancellation.cancelled:
			event = self._store.transition_with_event(
				live.record.run_id,
				status="CANCELLED",
				finished_at=finished_at,
				summary=summary,
				draft=RunEventDraft(type="run.cancelled", level="info", payload={"summary": summary, "cleanup": "completed"}),
			)
			await self._publish(live.record.run_id, event)
		else:
			event = self._store.transition_with_event(
				live.record.run_id,
				status="COMPLETED",
				finished_at=finished_at,
				summary=summary,
				draft=RunEventDraft(type="run.completed", level="info", payload={"summary": summary}),
			)
			await self._publish(live.record.run_id, event)

	async def _invoke_runner(self, live: _LiveRun) -> dict[str, Any]:
		"""Preserve compatibility with phase-1 two-argument fake runners."""

		run = self._runner.run
		parameters = inspect.signature(run).parameters
		if "cancellation" in parameters or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
			return await run(
				live.record.spec,
				lambda draft: self._emit(live, draft),
				cancellation=live.cancellation,
				output_dir=Path(live.record.output_dir),
			)
		return await run(live.record.spec, lambda draft: self._emit(live, draft))

	async def _emit(self, live: _LiveRun, draft: RunEventDraft) -> None:
		# RunManager owns the durable run state transition and its terminal events.
		if draft.type in {"run.started", "run.completed", "run.cancelled"}:
			return
		await self._append_event(live, draft)

	async def _append_event(self, live: _LiveRun, draft: RunEventDraft) -> None:
		event = self._store.append_event(live.record.run_id, draft)
		await self._publish(live.record.run_id, event)

	async def _publish(self, run_id: str, event: RunEvent) -> None:
		for queue in tuple(self._subscribers.get(run_id, ())):
			if queue.full():
				try:
					queue.get_nowait()
				except asyncio.QueueEmpty:
					pass
			queue.put_nowait(event)


class SidecarDrainingError(RuntimeError):
	"""Raised when the supervisor has started graceful shutdown."""


def _database_state_dir(database: Path | str) -> Path:
	if str(database) == ":memory:":
		return Path(".webretriever-desktop").resolve()
	return Path(database).expanduser().resolve().parent


def _project_storage_paths(records: list[StoredRun], state_dir: Path) -> list[Path]:
	"""Return only concrete descendants of the two sidecar-managed storage roots."""

	roots = {
		"task-submissions": (state_dir / "task-submissions").resolve(),
		"outputs": (state_dir / "outputs").resolve(),
	}
	paths: list[Path] = []
	seen: set[Path] = set()
	for record in records:
		for raw_path, root in ((record.spec.input_path, roots["task-submissions"]), (record.output_dir, roots["outputs"])):
			path = Path(raw_path).expanduser()
			resolved = path.resolve(strict=False)
			try:
				resolved.relative_to(root)
			except ValueError as exc:
				raise ProjectPathUnsafeError(raw_path) from exc
			if resolved == root or path.is_symlink():
				raise ProjectPathUnsafeError(raw_path)
			if resolved not in seen:
				seen.add(resolved)
				paths.append(resolved)
	return paths


def _path_exists(path: Path) -> bool:
	return path.exists() or path.is_symlink()


def _instruction_from_task_file(input_path: str) -> str | None:
	"""Recover the workspace prompt without exposing the task-file path on the wire."""

	try:
		payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError):
		return None
	if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
		return None
	instruction = payload[0].get("task")
	return instruction if isinstance(instruction, str) and instruction else None


def _accepted(record: StoredRun) -> RunAccepted:
	return RunAccepted(
		run_id=record.run_id,
		created_at=record.created_at,
		snapshot_url=f"/api/v1/runs/{record.run_id}",
		events_url=f"/api/v1/runs/{record.run_id}/events",
	)


__all__ = [
	"ActiveRunExistsError",
	"ActiveProjectRunError",
	"DesktopRunner",
	"EventsExpiredError",
	"IdempotencyKeyReusedError",
	"RunManager",
	"RunNotFoundError",
	"ProjectDeletionPermissionError",
	"ProjectNotFoundError",
	"ProjectPathUnsafeError",
	"SidecarDrainingError",
]
