"""Competition-compatible, crash-safe WebRetriever artifact management."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel

from browser_use.webretriever.models import CompetitionTask

MODEL_PROMPT_LOG_FILENAME = 'model_prompts.json'

try:
	import fcntl
except ImportError:  # pragma: no cover - the official runner is Linux
	fcntl = None  # type: ignore[assignment]


def _json_default(value: Any) -> Any:
	if isinstance(value, BaseModel):
		return value.model_dump(mode='json')
	if isinstance(value, Path):
		return str(value)
	if isinstance(value, (datetime, Enum)):
		return value.isoformat() if isinstance(value, datetime) else value.value
	raise TypeError(f'{type(value).__name__} is not JSON serializable')


def _fsync_directory(path: Path) -> None:
	"""Best-effort durability for the rename performed by atomic_write_json."""

	flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
	try:
		directory_fd = os.open(path, flags)
	except OSError:
		return
	try:
		os.fsync(directory_fd)
	except OSError:
		pass
	finally:
		os.close(directory_fd)


def atomic_write_json(path: Path | str, payload: Any) -> Path:
	"""Write UTF-8 JSON using an fsynced temporary file and atomic replace.

	The temporary file is created beside the destination, so ``os.replace``
	stays on one filesystem.  A failed serialization leaves any previous output
	untouched and removes its temporary file.
	"""

	destination = Path(path)
	destination.parent.mkdir(parents=True, exist_ok=True)
	temporary_path: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(
			mode='w',
			encoding='utf-8',
			dir=destination.parent,
			prefix=f'.{destination.name}.',
			suffix='.tmp',
			delete=False,
		) as temporary_file:
			temporary_path = Path(temporary_file.name)
			json.dump(payload, temporary_file, ensure_ascii=False, indent=2, default=_json_default)
			temporary_file.write('\n')
			temporary_file.flush()
			os.fsync(temporary_file.fileno())
		os.replace(temporary_path, destination)
		temporary_path = None
		_fsync_directory(destination.parent)
	except Exception:
		if temporary_path is not None:
			try:
				temporary_path.unlink()
			except FileNotFoundError:
				pass
		raise
	return destination


class TaskLock:
	"""An advisory per-task lock whose marker file may safely persist.

	Lock ownership is held by the open file descriptor, not by the existence of
	the ``.lock`` file.  A process crash therefore releases the lock while the
	marker remains useful for diagnostics and does not block future runs.
	"""

	def __init__(self, path: Path | str) -> None:
		self.path = Path(path)
		self._handle: TextIO | None = None

	@property
	def acquired(self) -> bool:
		return self._handle is not None

	def acquire(self, *, blocking: bool = False) -> bool:
		if self._handle is not None:
			return True
		if fcntl is None:  # pragma: no cover - official environment provides fcntl
			raise RuntimeError('TaskLock requires fcntl support')

		self.path.parent.mkdir(parents=True, exist_ok=True)
		handle = self.path.open('a+', encoding='utf-8')
		operation = fcntl.LOCK_EX
		if not blocking:
			operation |= fcntl.LOCK_NB
		try:
			fcntl.flock(handle.fileno(), operation)
		except BlockingIOError:
			handle.close()
			return False

		self._handle = handle
		try:
			handle.seek(0)
			handle.truncate()
			json.dump(
				{
					'pid': os.getpid(),
					'acquired_at': datetime.now(timezone.utc).isoformat(),
				},
				handle,
				ensure_ascii=False,
			)
			handle.write('\n')
			handle.flush()
			os.fsync(handle.fileno())
		except Exception:
			self._handle = None
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
			handle.close()
			raise
		return True

	def release(self, *, remove: bool = False) -> None:
		handle = self._handle
		if handle is not None:
			self._handle = None
			if fcntl is not None:
				fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
			handle.close()
		if remove:
			try:
				self.path.unlink()
			except FileNotFoundError:
				pass

	def __enter__(self) -> TaskLock:
		if not self.acquire(blocking=True):  # pragma: no cover - blocking locks either acquire or raise
			raise BlockingIOError(f'could not acquire task lock {self.path}')
		return self

	def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
		self.release()


class TaskArtifactWriter:
	"""Create and update all filesystem artifacts for one competition task."""

	def __init__(self, output_dir: Path | str, task: CompetitionTask) -> None:
		self.output_dir = Path(output_dir)
		self.task = task
		self.task_dir = self.output_dir / task.directory_name
		self.trajectory_dir = self.task_dir / 'trajectory'
		self.trajectory_visual_dir = self.task_dir / 'trajectory_visual'
		self.result_path = self.task_dir / 'result.json'
		self.capture_path = self.task_dir / 'capture.json'
		self.model_prompt_log_path = self.task_dir / MODEL_PROMPT_LOG_FILENAME
		self.logs_dir = self.output_dir / 'logs'
		self.lock_path = self.output_dir / 'locks' / f'{task.directory_name}.lock'
		self.lock = TaskLock(self.lock_path)

	def _pending_result(self) -> dict[str, Any]:
		return {
			**self.task.prompt_payload(),
			'status': 'PENDING',
			'actions': [],
			'thoughts': [],
			'urls': [],
			'agent_answer': '',
		}

	@staticmethod
	def _empty_capture() -> dict[str, Any]:
		return {
			'capture_time': datetime.now(timezone.utc).isoformat(),
			'total_requests': 0,
			'all_requests': [],
		}

	@staticmethod
	def _empty_model_prompt_log() -> dict[str, Any]:
		"""Return the initial model-prompt log for a task with no model calls."""

		return {
			'format': 'webretriever-model-prompts/v1',
			'system_prompt': '',
			'steps': [],
		}

	def prepare(self) -> Path:
		"""Idempotently create the task tree and initial JSON documents."""

		self.trajectory_dir.mkdir(parents=True, exist_ok=True)
		self.trajectory_visual_dir.mkdir(parents=True, exist_ok=True)
		self.logs_dir.mkdir(parents=True, exist_ok=True)
		self.lock_path.parent.mkdir(parents=True, exist_ok=True)
		self.lock_path.touch(exist_ok=True)

		already_acquired = self.lock.acquired
		if not already_acquired:
			self.lock.acquire(blocking=True)
		try:
			if not self.result_path.exists():
				atomic_write_json(self.result_path, self._pending_result())
			if not self.capture_path.exists():
				atomic_write_json(self.capture_path, self._empty_capture())
			if not self.model_prompt_log_path.exists():
				atomic_write_json(self.model_prompt_log_path, self._empty_model_prompt_log())
		finally:
			if not already_acquired:
				self.lock.release()
		return self.task_dir

	def acquire_lock(self, *, blocking: bool = False) -> bool:
		"""Acquire this task's advisory lock; historical files never block it."""

		self.lock_path.parent.mkdir(parents=True, exist_ok=True)
		return self.lock.acquire(blocking=blocking)

	def release_lock(self, *, remove: bool = False) -> None:
		self.lock.release(remove=remove)

	def trajectory_path(self, step: int, *, visual: bool = False, suffix: str = '.png') -> Path:
		"""Return a validated path for a numbered trajectory image."""

		if isinstance(step, bool) or not isinstance(step, int) or step < 0:
			raise ValueError('step must be a non-negative integer')
		if not suffix.startswith('.') or '/' in suffix or '\\' in suffix or suffix in {'.', '..'}:
			raise ValueError('suffix must be a safe file extension beginning with a dot')
		directory = self.trajectory_visual_dir if visual else self.trajectory_dir
		directory.mkdir(parents=True, exist_ok=True)
		return directory / f'{step}{suffix}'

	def write_result(self, payload: Mapping[str, Any] | None = None, **updates: Any) -> Path:
		"""Atomically write ``result.json`` with canonical task identity fields."""

		self.prepare()
		result = self._pending_result()
		incoming: dict[str, Any] = {}
		if payload is not None:
			incoming.update(dict(payload))
		incoming.update(updates)

		# Require the unambiguous official key for a model-produced answer.  The
		# public task data also calls its private reference value ``answer``, so
		# accepting that alias here would create an avoidable leakage footgun.
		for key in ('answer', 'ground_truth', 'ground_truth_answer', 'gold_answer', 'reference_answer'):
			incoming.pop(key, None)
		result.update(incoming)
		result.update(self.task.prompt_payload())
		return atomic_write_json(self.result_path, result)

	def write_capture(
		self,
		payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
	) -> Path:
		"""Atomically write the official XHR/fetch capture document."""

		self.prepare()
		if payload is None:
			capture = self._empty_capture()
		elif isinstance(payload, Mapping):
			capture = dict(payload)
			requests = capture.get('all_requests', [])
			if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
				raise ValueError('capture all_requests must be a sequence')
			requests = list(requests)
			capture['all_requests'] = requests
			capture.setdefault('capture_time', datetime.now(timezone.utc).isoformat())
			capture['total_requests'] = len(requests)
		else:
			requests = list(payload)
			capture = {
				'capture_time': datetime.now(timezone.utc).isoformat(),
				'total_requests': len(requests),
				'all_requests': requests,
			}
		return atomic_write_json(self.capture_path, capture)


def prepare_task_directory(output_dir: Path | str, task: CompetitionTask) -> Path:
	"""Create the official per-task output structure and return its directory."""

	return TaskArtifactWriter(output_dir, task).prepare()


__all__ = ['TaskArtifactWriter', 'TaskLock', 'atomic_write_json', 'prepare_task_directory']
