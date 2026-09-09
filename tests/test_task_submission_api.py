import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from browser_use.webretriever.desktop.api import create_app
from browser_use.webretriever.desktop.contracts import PreflightResult, RunAccepted


class RecordingManager:
	def __init__(self) -> None:
		self.spec = None
		self.idempotency_key = None

	async def create_run(self, *, spec, idempotency_key: str) -> RunAccepted:
		self.spec = spec
		self.idempotency_key = idempotency_key
		return RunAccepted(
			run_id="run-7",
			created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
			snapshot_url="/api/v1/runs/run-7",
			events_url="/api/v1/runs/run-7/events",
		)


def test_task_submission_creates_a_headed_run_from_the_workspace_prompt(tmp_path: Path) -> None:
	manager = RecordingManager()
	preflighted_specs = []

	async def preflight(spec):
		preflighted_specs.append(spec)
		return PreflightResult(task_count=1)

	client = TestClient(
		create_app(
			manager=manager,
			launch_token="test-token",
			preflight=preflight,
			task_submission_dir=tmp_path,
		)
	)

	response = client.post(
		"/api/v1/task-submissions",
		headers={"Authorization": "Bearer test-token", "Idempotency-Key": "2cfe9b3a-dd2f-4fa4-81cf-e99b4c508568"},
		json={"task": "整理首页信息", "website_url": "https://example.com"},
	)

	assert response.status_code == 202
	assert manager.spec is not None
	assert manager.spec is preflighted_specs[0]
	assert manager.spec.project_url == "https://example.com"
	assert manager.spec.browser.headed is True
	assert manager.spec.model.profile_id == "local-default"
	assert Path(manager.spec.output_root) == tmp_path / "outputs"
	assert json.loads(Path(manager.spec.input_path).read_text(encoding="utf-8")) == [
		{
			"task_idx": 0,
			"task_id": Path(manager.spec.input_path).stem,
			"website": "https://example.com",
			"task": "整理首页信息",
		}
	]
