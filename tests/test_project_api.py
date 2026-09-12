from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import browser_use.webretriever.desktop.run_manager as run_manager_module
from browser_use.webretriever.desktop.api import create_app
from browser_use.webretriever.desktop.contracts import RunEventDraft, RunSpec
from browser_use.webretriever.desktop.run_manager import ActiveProjectRunError, RunManager
from browser_use.webretriever.desktop.store import SqliteControlStore, StoredRun


class NoopRunner:
	async def run(self, *_args, **_kwargs):
		return {"ok": True}


def _seed_project_run(tmp_path: Path, *, status: str = "COMPLETED", output_dir: Path | None = None) -> Path:
	database = tmp_path / "control.sqlite3"
	input_path = tmp_path / "task-submissions" / "task-1.json"
	input_path.parent.mkdir(parents=True)
	input_path.write_text('[{"task": "整理项目首页"}]', encoding="utf-8")
	run_output = output_dir or tmp_path / "outputs" / "run-1"
	run_output.mkdir(parents=True)
	(run_output / "interaction.json").write_text("{}", encoding="utf-8")
	spec = RunSpec(
		schema_version=1,
		input_path=str(input_path),
		output_root=str(tmp_path / "outputs"),
		project_id="project-1",
		project_url="https://example.com",
		model={"profile_id": "local-default"},
		browser={"mode": "local", "headed": False},
	)
	store = SqliteControlStore(database)
	store.ensure_project("project-1", "https://example.com")
	store.create_run_with_event(
		StoredRun(
			run_id="run-1",
			idempotency_key="idempotency-1",
			project_id="project-1",
			spec=spec,
			spec_digest="digest",
			status=status,
			created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
			started_at=None,
			finished_at=None,
			output_dir=str(run_output),
			summary=None,
			error=None,
		),
		RunEventDraft(type="run.accepted", level="info"),
	)
	store.close()
	return database


def test_project_history_and_delete_remove_owned_files_and_records(tmp_path: Path) -> None:
	database = _seed_project_run(tmp_path)
	manager = RunManager(runner=NoopRunner(), database_path=database, state_dir=tmp_path)
	client = TestClient(create_app(manager=manager, launch_token="test-token", state_dir=tmp_path))
	headers = {"Authorization": "Bearer test-token"}

	history = client.get("/api/v1/projects/project-1/history", headers=headers)
	assert history.status_code == 200
	assert history.json()["items"][0]["run_id"] == "run-1"
	assert history.json()["items"][0]["instruction"] == "整理项目首页"
	assert history.json()["items"][0]["events"][0]["type"] == "run.accepted"

	response = client.delete("/api/v1/projects/project-1", headers=headers)
	assert response.status_code == 204
	assert not (tmp_path / "task-submissions" / "task-1.json").exists()
	assert not (tmp_path / "outputs" / "run-1").exists()
	assert client.get("/api/v1/projects/project-1/history", headers=headers).json()["error_code"] == "project_not_found"

	store = SqliteControlStore(database)
	assert store.get_project("project-1") is None
	assert store.get_run("run-1") is None
	assert store.events_after("run-1", after=0) == []
	store.close()


def test_delete_project_rejects_active_run_with_stable_problem_code(tmp_path: Path) -> None:
	class ActiveManager:
		async def delete_project(self, _project_id: str, *, state_dir: str) -> None:
			raise ActiveProjectRunError("run-1")

	manager = ActiveManager()
	client = TestClient(create_app(manager=manager, launch_token="test-token", state_dir=tmp_path))
	response = client.delete(
		"/api/v1/projects/project-1",
		headers={"Authorization": "Bearer test-token"},
	)
	assert response.status_code == 409
	assert response.json()["error_code"] == "project_has_active_run"
	assert response.json()["run_id"] == "run-1"


def test_delete_project_rejects_paths_outside_managed_state(tmp_path: Path) -> None:
	external_dir = tmp_path / "external-output"
	database = _seed_project_run(tmp_path, output_dir=external_dir)
	manager = RunManager(runner=NoopRunner(), database_path=database, state_dir=tmp_path)
	client = TestClient(create_app(manager=manager, launch_token="test-token", state_dir=tmp_path))
	response = client.delete(
		"/api/v1/projects/project-1",
		headers={"Authorization": "Bearer test-token"},
	)
	assert response.status_code == 409
	assert response.json()["error_code"] == "project_path_unsafe"
	assert external_dir.is_dir()


def test_delete_project_returns_a_sanitized_problem_when_cleanup_fails(tmp_path: Path) -> None:
	class FailingManager:
		async def delete_project(self, _project_id: str, *, state_dir: str) -> None:
			raise PermissionError("an open file must not leak into the response")

	client = TestClient(
		create_app(
			manager=FailingManager(),
			launch_token="test-token",
			state_dir=tmp_path,
			allowed_origins=("http://localhost:1420",),
		),
		raise_server_exceptions=False,
	)
	response = client.delete(
		"/api/v1/projects/project-1",
		headers={"Authorization": "Bearer test-token", "Origin": "http://localhost:1420"},
	)

	assert response.status_code == 500
	assert response.json()["error_code"] == "project_delete_failed"
	assert response.json()["errors"] is None
	assert response.headers["access-control-allow-origin"] == "http://localhost:1420"
	assert "open file" not in response.text


def test_delete_project_identifies_a_permission_error_while_staging_files(tmp_path: Path, monkeypatch) -> None:
	database = _seed_project_run(tmp_path)
	manager = RunManager(runner=NoopRunner(), database_path=database, state_dir=tmp_path)

	def reject_move(*_args, **_kwargs) -> None:
		raise PermissionError("locked file")

	monkeypatch.setattr(run_manager_module.shutil, "move", reject_move)
	client = TestClient(create_app(manager=manager, launch_token="test-token", state_dir=tmp_path))
	response = client.delete(
		"/api/v1/projects/project-1",
		headers={"Authorization": "Bearer test-token"},
	)

	assert response.status_code == 409
	assert response.json()["error_code"] == "project_files_in_use"
	assert response.json()["errors"] == {"diagnostic": ["stage_files"]}
	assert "locked file" not in response.text


def test_delete_project_returns_not_found_for_unknown_project(tmp_path: Path) -> None:
	manager = RunManager(runner=NoopRunner(), database_path=tmp_path / "control.sqlite3", state_dir=tmp_path)
	client = TestClient(create_app(manager=manager, launch_token="test-token", state_dir=tmp_path))
	response = client.delete(
		"/api/v1/projects/missing",
		headers={"Authorization": "Bearer test-token"},
	)
	assert response.status_code == 404
	assert response.json()["error_code"] == "project_not_found"
