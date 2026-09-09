import asyncio
import json

import pytest
from pydantic import ValidationError

from browser_use.webretriever.desktop.contracts import RunSpec
from browser_use.webretriever.desktop.runner_adapter import RunnerAdapter, RunnerProfile, StaticProfileResolver
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.run_control import CancellationToken
from browser_use.webretriever.runner import RunnerConfig, run


def make_spec(project_url: str | None) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "input_path": "tasks.json",
        "output_root": "outputs",
        "model": {"profile_id": "local-default"},
        "browser": {"mode": "local", "headed": False},
    }
    if project_url is not None:
        spec["project_url"] = project_url
    return spec


def test_run_spec_keeps_the_project_url_as_run_context() -> None:
    spec = RunSpec.model_validate(make_spec("https://example.com"))

    assert spec.project_url == "https://example.com"


def test_run_spec_rejects_a_non_http_project_url() -> None:
    with pytest.raises(ValidationError):
        RunSpec.model_validate(make_spec("example.com"))


def test_runner_adapter_passes_project_url_to_the_execution_boundary(tmp_path) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text(
        json.dumps([{"task_idx": 0, "task_id": "task-0", "website": "https://other.example", "task": "inspect"}]),
        encoding="utf-8",
    )
    spec = RunSpec.model_validate(
        {
            **make_spec("https://example.com"),
            "input_path": str(task_file),
            "output_root": str(tmp_path),
            "browser": {"mode": "local", "headed": True},
        }
    )
    captured = {}

    async def fake_runner(config, **_kwargs):
        captured["config"] = config
        return {}

    async def emit(_event):
        return None

    adapter = RunnerAdapter(
        profiles=StaticProfileResolver(
            {
                "local-default": RunnerProfile(
                    model="test-model",
                    model_services=(ModelServiceConfig("local", "http://127.0.0.1:1/v1", "test-key"),),
                )
            }
        ),
        runner=fake_runner,
    )

    asyncio.run(
        adapter.run(
            spec,
            emit,
            cancellation=CancellationToken(),
            output_dir=tmp_path / "run",
        )
    )

    assert captured["config"].project_url == "https://example.com"
    assert captured["config"].headless is False


def test_runner_scopes_cancelled_task_artifacts_to_the_project_url(tmp_path) -> None:
    task_file = tmp_path / "tasks.json"
    task_file.write_text(
        json.dumps([{"task_idx": 0, "task_id": "task-0", "website": "https://other.example", "task": "inspect"}]),
        encoding="utf-8",
    )
    output_dir = tmp_path / "run"
    cancellation = CancellationToken()
    cancellation.cancel("test")
    config = RunnerConfig(
        input_path=task_file,
        output_dir=output_dir,
        model="test-model",
        cdp_urls=[],
        model_services=[ModelServiceConfig("local", "http://127.0.0.1:1/v1", "test-key")],
        local_browser=True,
        project_url="https://example.com",
    )

    asyncio.run(run(config, cancellation=cancellation))

    result = json.loads((output_dir / "0_task-0" / "result.json").read_text(encoding="utf-8"))
    assert result["website"] == "https://example.com"
