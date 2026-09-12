"""SQLite persistence for the sidecar's small, durable control plane."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from browser_use.webretriever.desktop.contracts import ProjectSummary, RunEvent, RunEventDraft, RunSnapshot, RunSpec
from browser_use.webretriever.desktop.versioning import SQLITE_SCHEMA_VERSION


_ACTIVE_STATUSES = ("STARTING", "RUNNING", "CANCELLING")


@dataclass(frozen=True, slots=True)
class StoredRun:
	run_id: str
	idempotency_key: str
	spec: RunSpec
	spec_digest: str
	status: str
	created_at: datetime
	started_at: datetime | None
	finished_at: datetime | None
	output_dir: str
	summary: dict[str, Any] | None
	error: dict[str, Any] | None
	project_id: str | None = None

	def snapshot(self, *, last_event_id: int) -> RunSnapshot:
		return RunSnapshot(
			run_id=self.run_id,
			project_id=self.project_id,
			project_url=self.spec.project_url,
			status=self.status,
			created_at=self.created_at,
			started_at=self.started_at,
			finished_at=self.finished_at,
			output_dir=self.output_dir,
			last_event_id=last_event_id,
			summary=self.summary,
			error=self.error,
		)


@dataclass(frozen=True, slots=True)
class StoredProject:
	project_id: str
	website_url: str
	created_at: datetime

	def summary(self) -> ProjectSummary:
		return ProjectSummary(
			project_id=self.project_id,
			website_url=self.website_url,
			created_at=self.created_at,
		)


class ProjectUrlMismatchError(ValueError):
	"""Raised when an existing project id is asserted for another URL."""


class SqliteControlStore:
	"""Single-process SQLite WAL journal with atomic run/event transitions."""

	def __init__(self, database_path: Path | str, *, event_retention: int = 10_000) -> None:
		if event_retention < 1:
			raise ValueError("event_retention must be at least one")
		self._event_retention = event_retention
		self._database_path = str(database_path)
		if self._database_path != ":memory:":
			Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
		# The production sidecar uses one event loop/thread.  Allowing a TestClient
		# portal to own that loop still preserves SQLite's transaction semantics and
		# lets construction happen in the calling test thread.
		self._connection = sqlite3.connect(self._database_path, isolation_level=None, check_same_thread=False)
		self._connection.row_factory = sqlite3.Row
		self._connection.execute("PRAGMA journal_mode=WAL")
		self._connection.execute("PRAGMA foreign_keys=ON")
		self._connection.execute("PRAGMA synchronous=FULL")
		self._migrate()

	def close(self) -> None:
		self._connection.close()

	def _migrate(self) -> None:
		current_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
		if current_version > SQLITE_SCHEMA_VERSION:
			raise RuntimeError(
				f"control database schema {current_version} is newer than this sidecar ({SQLITE_SCHEMA_VERSION})"
			)
		if current_version not in {0, 1} and current_version != SQLITE_SCHEMA_VERSION:
			raise RuntimeError(f"unsupported control database schema {current_version}")
		self._connection.executescript(
			"""
			CREATE TABLE IF NOT EXISTS projects (
				project_id TEXT PRIMARY KEY,
				website_url TEXT NOT NULL,
				created_at TEXT NOT NULL
			);
			CREATE TABLE IF NOT EXISTS runs (
				run_id TEXT PRIMARY KEY,
				idempotency_key TEXT NOT NULL UNIQUE,
				project_id TEXT,
				spec_json TEXT NOT NULL,
				spec_digest TEXT NOT NULL,
				status TEXT NOT NULL,
				created_at TEXT NOT NULL,
				started_at TEXT,
				finished_at TEXT,
				output_dir TEXT NOT NULL,
				last_event_id INTEGER NOT NULL DEFAULT 0,
				summary_json TEXT,
				error_json TEXT
			);
			CREATE INDEX IF NOT EXISTS runs_active_status ON runs(status);
			CREATE TABLE IF NOT EXISTS events (
				run_id TEXT NOT NULL REFERENCES runs(run_id),
				event_id INTEGER NOT NULL,
				type TEXT NOT NULL,
				occurred_at TEXT NOT NULL,
				level TEXT NOT NULL,
				task_json TEXT,
				payload_json TEXT NOT NULL,
				PRIMARY KEY (run_id, event_id)
			);
			CREATE INDEX IF NOT EXISTS events_replay ON events(run_id, event_id);
			"""
		)
		columns = {str(row["name"]) for row in self._connection.execute("PRAGMA table_info(runs)")}
		if "last_event_id" not in columns:
			self._connection.execute("ALTER TABLE runs ADD COLUMN last_event_id INTEGER NOT NULL DEFAULT 0")
		if "project_id" not in columns:
			self._connection.execute("ALTER TABLE runs ADD COLUMN project_id TEXT")
		self._connection.execute("CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id, created_at DESC, run_id DESC)")
		self._connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

	def get_project(self, project_id: str) -> StoredProject | None:
		row = self._connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
		return _stored_project(row) if row is not None else None

	def ensure_project(self, project_id: str, website_url: str) -> StoredProject:
		current = self.get_project(project_id)
		if current is not None:
			if current.website_url != website_url:
				raise ProjectUrlMismatchError(project_id)
			return current
		created_at = datetime.now(timezone.utc)
		self._connection.execute(
			"INSERT INTO projects (project_id, website_url, created_at) VALUES (?, ?, ?)",
			(project_id, website_url, _datetime(created_at)),
		)
		return StoredProject(project_id=project_id, website_url=website_url, created_at=created_at)

	def list_projects(self, *, limit: int = 100) -> list[StoredProject]:
		rows = self._connection.execute(
			"SELECT * FROM projects ORDER BY created_at DESC, project_id DESC LIMIT ?", (limit,)
		).fetchall()
		return [_stored_project(row) for row in rows]

	def create_run(self, record: StoredRun) -> None:
		self._connection.execute(
			"""
			INSERT INTO runs (
				run_id, idempotency_key, project_id, spec_json, spec_digest, status, created_at,
				started_at, finished_at, output_dir, last_event_id, summary_json, error_json
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
			""",
			(
				record.run_id,
				record.idempotency_key,
				record.project_id,
				_json(record.spec.model_dump(mode="json")),
				record.spec_digest,
				record.status,
				_datetime(record.created_at),
				_datetime(record.started_at),
				_datetime(record.finished_at),
				record.output_dir,
				_json(record.summary) if record.summary is not None else None,
				_json(record.error) if record.error is not None else None,
			),
		)

	def create_run_with_event(self, record: StoredRun, draft: RunEventDraft) -> RunEvent:
		"""Make acceptance and its first visible event one durable transaction."""

		self._connection.execute("BEGIN IMMEDIATE")
		try:
			self._connection.execute(
				"""
				INSERT INTO runs (
					run_id, idempotency_key, project_id, spec_json, spec_digest, status, created_at,
					started_at, finished_at, output_dir, last_event_id, summary_json, error_json
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
				""",
				(
					record.run_id,
					record.idempotency_key,
					record.project_id,
					_json(record.spec.model_dump(mode="json")),
					record.spec_digest,
					record.status,
					_datetime(record.created_at),
					_datetime(record.started_at),
					_datetime(record.finished_at),
					record.output_dir,
					_json(record.summary) if record.summary is not None else None,
					_json(record.error) if record.error is not None else None,
				),
			)
			event = self._append_event_in_transaction(record.run_id, draft)
			self._connection.execute("COMMIT")
			return event
		except BaseException:
			self._connection.execute("ROLLBACK")
			raise

	def get_run(self, run_id: str) -> StoredRun | None:
		row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
		return _stored_run(row) if row is not None else None

	def get_idempotency(self, key: str) -> StoredRun | None:
		row = self._connection.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
		return _stored_run(row) if row is not None else None

	def active_run(self) -> StoredRun | None:
		placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
		row = self._connection.execute(
			f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at LIMIT 1", _ACTIVE_STATUSES
		).fetchone()
		return _stored_run(row) if row is not None else None

	def list_runs(self, *, cursor: str | None, limit: int) -> tuple[list[StoredRun], str | None]:
		if cursor is None:
			rows = self._connection.execute(
				"SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit + 1,)
			).fetchall()
		else:
			cursor_row = self._connection.execute("SELECT created_at FROM runs WHERE run_id = ?", (cursor,)).fetchone()
			if cursor_row is None:
				return [], None
			rows = self._connection.execute(
				"""
				SELECT * FROM runs
				WHERE created_at < ? OR (created_at = ? AND run_id < ?)
				ORDER BY created_at DESC, run_id DESC LIMIT ?
				""",
				(cursor_row["created_at"], cursor_row["created_at"], cursor, limit + 1),
			).fetchall()
		stored = [_stored_run(row) for row in rows[:limit]]
		next_cursor = stored[-1].run_id if len(rows) > limit and stored else None
		return stored, next_cursor

	def list_project_runs(
		self,
		*,
		project_id: str,
		website_url: str,
		cursor: str | None,
		limit: int,
	) -> tuple[list[StoredRun], str | None]:
		"""List explicit project runs and legacy URL-owned runs in one stable page."""

		records = [
			_stored_run(row)
			for row in self._connection.execute("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC").fetchall()
		]
		owned = [
			record
			for record in records
			if record.project_id == project_id or (record.project_id is None and record.spec.project_url == website_url)
		]
		if cursor is not None:
			cursor_index = next((index for index, record in enumerate(owned) if record.run_id == cursor), None)
			if cursor_index is None:
				return [], None
			owned = owned[cursor_index + 1 :]
		page = owned[:limit]
		next_cursor = page[-1].run_id if len(owned) > limit and page else None
		return page, next_cursor

	def active_project_run(self, *, project_id: str, website_url: str) -> StoredRun | None:
		records, _ = self.list_project_runs(
			project_id=project_id,
			website_url=website_url,
			cursor=None,
			limit=10_000,
		)
		return next((record for record in records if record.status in _ACTIVE_STATUSES), None)

	def delete_project_records(self, project_id: str, run_ids: Iterable[str]) -> None:
		"""Delete events, runs, and the project atomically in SQLite."""

		ids = tuple(run_ids)
		self._connection.execute("BEGIN IMMEDIATE")
		try:
			if ids:
				placeholders = ", ".join("?" for _ in ids)
				self._connection.execute(f"DELETE FROM events WHERE run_id IN ({placeholders})", ids)
				self._connection.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", ids)
			self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
			self._connection.execute("COMMIT")
		except BaseException:
			self._connection.execute("ROLLBACK")
			raise

	def update_run(
		self,
		run_id: str,
		*,
		status: str,
		started_at: datetime | None = None,
		finished_at: datetime | None = None,
		summary: dict[str, Any] | None = None,
		error: dict[str, Any] | None = None,
	) -> None:
		current = self.get_run(run_id)
		if current is None:
			raise KeyError(run_id)
		self._connection.execute(
			"""
			UPDATE runs SET status = ?, started_at = ?, finished_at = ?, summary_json = ?, error_json = ?
			WHERE run_id = ?
			""",
			(
				status,
				_datetime(started_at if started_at is not None else current.started_at),
				_datetime(finished_at if finished_at is not None else current.finished_at),
				_json(summary) if summary is not None else (_json(current.summary) if current.summary is not None else None),
				_json(error) if error is not None else (_json(current.error) if current.error is not None else None),
				run_id,
			),
		)

	def transition_with_event(
		self,
		run_id: str,
		*,
		status: str,
		draft: RunEventDraft,
		started_at: datetime | None = None,
		finished_at: datetime | None = None,
		summary: dict[str, Any] | None = None,
		error: dict[str, Any] | None = None,
	) -> RunEvent:
		"""Atomically persist a status projection and its causally matching event."""

		self._connection.execute("BEGIN IMMEDIATE")
		try:
			current = self.get_run(run_id)
			if current is None:
				raise KeyError(run_id)
			self._connection.execute(
				"""
				UPDATE runs SET status = ?, started_at = ?, finished_at = ?, summary_json = ?, error_json = ?
				WHERE run_id = ?
				""",
				(
					status,
					_datetime(started_at if started_at is not None else current.started_at),
					_datetime(finished_at if finished_at is not None else current.finished_at),
					_json(summary) if summary is not None else (_json(current.summary) if current.summary is not None else None),
					_json(error) if error is not None else (_json(current.error) if current.error is not None else None),
					run_id,
				),
			)
			event = self._append_event_in_transaction(run_id, draft)
			self._connection.execute("COMMIT")
			return event
		except BaseException:
			self._connection.execute("ROLLBACK")
			raise

	def append_event(self, run_id: str, draft: RunEventDraft, *, occurred_at: datetime | None = None) -> RunEvent:
		self._connection.execute("BEGIN IMMEDIATE")
		try:
			event = self._append_event_in_transaction(run_id, draft, occurred_at=occurred_at)
			self._connection.execute("COMMIT")
			return event
		except BaseException:
			self._connection.execute("ROLLBACK")
			raise

	def _append_event_in_transaction(
		self, run_id: str, draft: RunEventDraft, *, occurred_at: datetime | None = None
	) -> RunEvent:
		when = occurred_at or datetime.now(timezone.utc)
		row = self._connection.execute("SELECT last_event_id + 1 AS next_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
		if row is None:
			raise KeyError(run_id)
		event = RunEvent(
			run_id=run_id,
			event_id=int(row["next_id"]),
			type=draft.type,
			occurred_at=when,
			level=draft.level,
			task=draft.task,
			payload=draft.payload,
		)
		self._connection.execute(
			"""
			INSERT INTO events (run_id, event_id, type, occurred_at, level, task_json, payload_json)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(
				run_id,
				event.event_id,
				event.type,
				_datetime(event.occurred_at),
				event.level,
				_json(event.task.model_dump(mode="json")) if event.task is not None else None,
				_json(event.payload),
			),
		)
		self._connection.execute("UPDATE runs SET last_event_id = ? WHERE run_id = ?", (event.event_id, run_id))
		oldest_allowed = event.event_id - self._event_retention
		if oldest_allowed > 0:
			self._connection.execute("DELETE FROM events WHERE run_id = ? AND event_id <= ?", (run_id, oldest_allowed))
		return event

	def events_after(self, run_id: str, *, after: int) -> list[RunEvent]:
		rows = self._connection.execute(
			"SELECT * FROM events WHERE run_id = ? AND event_id > ? ORDER BY event_id", (run_id, after)
		).fetchall()
		return [_event(row) for row in rows]

	def last_event_id(self, run_id: str) -> int:
		row = self._connection.execute("SELECT last_event_id AS last_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
		assert row is not None
		return int(row["last_id"])

	def minimum_event_id(self, run_id: str) -> int | None:
		row = self._connection.execute("SELECT MIN(event_id) AS minimum_id FROM events WHERE run_id = ?", (run_id,)).fetchone()
		assert row is not None
		return int(row["minimum_id"]) if row["minimum_id"] is not None else None

	def reconcile_interrupted(self) -> list[RunEvent]:
		"""Mark work left active by a dead sidecar as interrupted exactly once."""

		reconciled: list[RunEvent] = []
		for record in _iter_stored_runs(self._connection.execute("SELECT * FROM runs WHERE status IN (?, ?, ?)", _ACTIVE_STATUSES)):
			finished_at = datetime.now(timezone.utc)
			reconciled.append(
				self.transition_with_event(
					record.run_id,
					status="INTERRUPTED",
					finished_at=finished_at,
					draft=RunEventDraft(
						type="run.interrupted",
						level="warning",
						payload={"detected_at": finished_at.isoformat(), "previous_state": record.status},
					),
				)
			)
		return reconciled


def _stored_run(row: sqlite3.Row) -> StoredRun:
	return StoredRun(
		run_id=str(row["run_id"]),
		idempotency_key=str(row["idempotency_key"]),
		project_id=str(row["project_id"]) if row["project_id"] is not None else None,
		spec=RunSpec.model_validate_json(str(row["spec_json"])),
		spec_digest=str(row["spec_digest"]),
		status=str(row["status"]),
		created_at=_parse_datetime(str(row["created_at"])),
		started_at=_parse_datetime(row["started_at"]) if row["started_at"] is not None else None,
		finished_at=_parse_datetime(row["finished_at"]) if row["finished_at"] is not None else None,
		output_dir=str(row["output_dir"]),
		summary=_parse_json(row["summary_json"]),
		error=_parse_json(row["error_json"]),
	)


def _stored_project(row: sqlite3.Row) -> StoredProject:
	return StoredProject(
		project_id=str(row["project_id"]),
		website_url=str(row["website_url"]),
		created_at=_parse_datetime(str(row["created_at"])),
	)


def _iter_stored_runs(rows: Iterable[sqlite3.Row]) -> Iterable[StoredRun]:
	return (_stored_run(row) for row in rows)


def _event(row: sqlite3.Row) -> RunEvent:
	return RunEvent(
		run_id=str(row["run_id"]),
		event_id=int(row["event_id"]),
		type=str(row["type"]),
		occurred_at=_parse_datetime(str(row["occurred_at"])),
		level=str(row["level"]),
		task=_parse_json(row["task_json"]),
		payload=_parse_json(row["payload_json"]) or {},
	)


def _datetime(value: datetime | None) -> str | None:
	return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _parse_datetime(value: str) -> datetime:
	return datetime.fromisoformat(value).astimezone(timezone.utc)


def _json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse_json(value: str | None) -> dict[str, Any] | None:
	if value is None:
		return None
	loaded = json.loads(value)
	return loaded if isinstance(loaded, dict) else None


__all__ = [
	"ProjectUrlMismatchError",
	"SQLITE_SCHEMA_VERSION",
	"SqliteControlStore",
	"StoredProject",
	"StoredRun",
]
