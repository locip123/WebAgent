"""Translate a workspace prompt into one durable, headed Runner task."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from browser_use.webretriever.desktop.contracts import RunSpec, TaskSubmissionRequest


class TaskSubmissionService:
	"""Own the sidecar-local files backing task-composer submissions."""

	def __init__(self, state_dir: Path | str, *, profile_id: str = "local-default") -> None:
		self._state_dir = Path(state_dir)
		self._profile_id = profile_id

	def create_run_spec(self, submission: TaskSubmissionRequest) -> RunSpec:
		task_id = uuid4().hex
		task_dir = self._state_dir / "task-submissions"
		output_root = self._state_dir / "outputs"
		task_dir.mkdir(parents=True, exist_ok=True)
		output_root.mkdir(parents=True, exist_ok=True)
		task_path = task_dir / f"{task_id}.json"
		task_path.write_text(
			json.dumps(
				[
					{
						"task_idx": 0,
						"task_id": task_id,
						"website": submission.website_url,
						"task": submission.task,
					}
				],
				ensure_ascii=False,
			),
			encoding="utf-8",
		)
		return RunSpec(
			schema_version=1,
			input_path=str(task_path),
			output_root=str(output_root),
			project_id=submission.project_id,
			project_url=submission.website_url,
			model={"profile_id": self._profile_id},
			browser={"mode": "local", "headed": True},
		)


__all__ = ["TaskSubmissionService"]
