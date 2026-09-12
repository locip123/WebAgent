"""Executable FastAPI sidecar with the fixed stdout bootstrap handshake."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
import socket
from pathlib import Path
import sys
from typing import Sequence

import uvicorn

from browser_use.webretriever.desktop.api import create_app
from browser_use.webretriever.desktop.account_profile_store import AccountProfileStore
from browser_use.webretriever.desktop.model_service_store import ModelServiceStore
from browser_use.webretriever.desktop.run_manager import RunManager
from browser_use.webretriever.desktop.runner_adapter import JsonProfileResolver, RunnerAdapter
from browser_use.webretriever.desktop.versioning import SIDECAR_PROTOCOL_VERSION


PROTOCOL_VERSION = SIDECAR_PROTOCOL_VERSION
_RELEASE_FORMAT = "webretriever.desktop-release/v1"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run the WebRetriever local FastAPI sidecar.")
	parser.add_argument("--state-dir", type=Path, default=os.getenv("WR_SIDECAR_STATE_DIR"))
	parser.add_argument("--log-level", choices=("critical", "error", "warning", "info"), default="warning")
	return parser.parse_args(argv)


def _environment_mapping(name: str) -> dict[str, str]:
	raw = os.getenv(name, "")
	if not raw:
		return {}
	try:
		value = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise ValueError(f"{name} must be a JSON object") from exc
	if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(path, str) for key, path in value.items()):
		raise ValueError(f"{name} must map profile ids to local config paths")
	return value


async def _serve(*, state_dir: Path, launch_token: str, launch_nonce: str, log_level: str) -> None:
	sidecar_build, runner_build = _runtime_build_info()
	model_config_path = _model_service_config_path(state_dir)
	profile_paths = _environment_mapping("WR_SIDECAR_PROFILE_CONFIGS_JSON")
	profile_paths.setdefault("local-default", str(model_config_path))
	profiles = JsonProfileResolver(profile_paths)
	adapter = RunnerAdapter(profiles=profiles)
	manager = RunManager(runner=adapter, database_path=state_dir / "control.sqlite3", state_dir=state_dir)
	shutdown_requested = asyncio.Event()

	async def notify_shutdown() -> None:
		shutdown_requested.set()

	allowed_origins = tuple(origin.strip() for origin in os.getenv("WR_SIDECAR_ALLOWED_ORIGINS", "").split(",") if origin.strip())
	app = create_app(
		manager=manager,
		launch_token=launch_token,
		preflight=adapter.preflight,
		allowed_origins=allowed_origins,
		on_shutdown_requested=notify_shutdown,
		account_profile_store=AccountProfileStore(state_dir / "account-profile.json"),
		model_service_store=ModelServiceStore(model_config_path),
		sidecar_build=sidecar_build,
		runner_build=runner_build,
		task_submission_dir=state_dir,
		state_dir=state_dir,
	)
	listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	listener.bind(("127.0.0.1", 0))
	listener.listen(socket.SOMAXCONN)
	port = int(listener.getsockname()[1])
	print(
		"WR_SIDECAR_LISTENING "
		+ json.dumps(
			{"protocol_version": PROTOCOL_VERSION, "port": port, "pid": os.getpid(), "launch_nonce": launch_nonce},
			separators=(",", ":"),
		),
		flush=True,
	)
	config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level, access_log=False)
	server = uvicorn.Server(config)

	async def stop_after_drain() -> None:
		await shutdown_requested.wait()
		with suppress(asyncio.TimeoutError):
			await asyncio.wait_for(manager.wait_for_idle(), timeout=65)
		server.should_exit = True

	shutdown_task = asyncio.create_task(stop_after_drain(), name="sidecar-drain-shutdown")
	try:
		await server.serve(sockets=[listener])
	finally:
		shutdown_task.cancel()
		with suppress(asyncio.CancelledError):
			await shutdown_task
		listener.close()


def _model_service_config_path(state_dir: Path) -> Path:
	"""Use the supplied config in releases and the repository config during development."""

	configured_path = os.getenv("WR_SIDECAR_MODEL_CONFIG_PATH")
	if configured_path:
		return Path(configured_path).expanduser().resolve()
	repository_config = Path(__file__).resolve().parents[4] / "config.json"
	return repository_config if repository_config.is_file() else state_dir / "config.json"


def _runtime_build_info() -> tuple[str, str]:
	"""Load release provenance and force Playwright to use the bundled revision."""

	manifest_path = Path(sys.executable).resolve().parent / "release-manifest.json"
	if not manifest_path.is_file():
		return "development", "development"
	try:
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise RuntimeError("bundled sidecar release manifest is unreadable") from exc
	if not isinstance(manifest, dict) or manifest.get("format") != _RELEASE_FORMAT:
		raise RuntimeError("bundled sidecar release manifest has an unsupported format")
	build = manifest.get("build")
	if not isinstance(build, dict):
		raise RuntimeError("bundled sidecar release manifest has no build matrix")
	sidecar_build = build.get("sidecar")
	runner_build = build.get("runner")
	if not isinstance(sidecar_build, str) or not sidecar_build or not isinstance(runner_build, str) or not runner_build:
		raise RuntimeError("bundled sidecar release manifest has an invalid build matrix")
	browsers = manifest_path.parent / "playwright-browsers"
	if not browsers.is_dir():
		raise RuntimeError("bundled sidecar is missing its Playwright browser directory")
	os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
	return sidecar_build, runner_build


def main(argv: Sequence[str] | None = None) -> int:
	args = _parse_args(argv)
	if args.state_dir is None:
		raise SystemExit("--state-dir or WR_SIDECAR_STATE_DIR is required")
	launch_token = os.getenv("WR_SIDECAR_BEARER_TOKEN", "")
	launch_nonce = os.getenv("WR_SIDECAR_LAUNCH_NONCE", "")
	if not launch_token or not launch_nonce:
		raise SystemExit("WR_SIDECAR_BEARER_TOKEN and WR_SIDECAR_LAUNCH_NONCE are required")
	state_dir = Path(args.state_dir).expanduser().resolve()
	state_dir.mkdir(parents=True, exist_ok=True)
	asyncio.run(_serve(state_dir=state_dir, launch_token=launch_token, launch_nonce=launch_nonce, log_level=args.log_level))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
