"""Public tooling for producing and checking self-contained desktop releases.

The command deliberately treats a release directory as a closed artifact: the
sidecar executable, its PyInstaller payload, and the Playwright browser are
all verified from the release manifest instead of relying on the developer's
Python or conda environment.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import import_module, metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import select
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import secrets

from browser_use.webretriever.desktop.versioning import API_PROTOCOL_VERSION, SIDECAR_PROTOCOL_VERSION, SQLITE_SCHEMA_VERSION


RELEASE_FORMAT = "webretriever.desktop-release/v1"
_MANIFEST_NAME = "release-manifest.json"
_SIDECAR_RELATIVE_PATH = PurePosixPath("sidecar/webretriever-sidecar")
_BROWSER_ROOT = PurePosixPath("sidecar/playwright-browsers")


class ReleaseValidationError(ValueError):
	"""A release directory cannot safely be shipped or started."""


def stage_bundle(
	*,
	sidecar_dist: Path | str,
	playwright_browsers_dir: Path | str,
	output_dir: Path | str,
	target: str,
	sidecar_build: str,
	runner_build: str,
) -> dict[str, Any]:
	"""Stage a PyInstaller ``onedir`` output and its exact Playwright browser set."""

	_validate_native_linux_target(target)
	if not sidecar_build or not runner_build:
		raise ReleaseValidationError("sidecar and runner build identifiers must be non-empty")
	source_sidecar = Path(sidecar_dist).expanduser().resolve()
	if not source_sidecar.is_dir():
		raise ReleaseValidationError(f"PyInstaller sidecar directory does not exist: {source_sidecar}")
	program = source_sidecar / _SIDECAR_RELATIVE_PATH.name
	if not program.is_file() or not os.access(program, os.X_OK):
		raise ReleaseValidationError("PyInstaller sidecar directory must contain an executable webretriever-sidecar")
	source_browsers = Path(playwright_browsers_dir).expanduser().resolve()
	expected_entries, playwright_version = _expected_playwright_browser_entries()
	for entry in expected_entries:
		if not (source_browsers / entry).is_dir():
			raise ReleaseValidationError(f"Playwright browser cache is missing required entry {entry!r}")

	destination = Path(output_dir).expanduser().resolve()
	if destination.exists():
		raise ReleaseValidationError(f"release output already exists: {destination}")
	destination.parent.mkdir(parents=True, exist_ok=True)
	temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
	try:
		staged_sidecar = temporary / "sidecar"
		shutil.copytree(source_sidecar, staged_sidecar, copy_function=shutil.copy2)
		staged_browsers = staged_sidecar / "playwright-browsers"
		staged_browsers.mkdir()
		for entry in expected_entries:
			shutil.copytree(source_browsers / entry, staged_browsers / entry, copy_function=shutil.copy2)
		manifest = {
			"format": RELEASE_FORMAT,
			"target": target,
			"build": {"app": _desktop_app_build(), "sidecar": sidecar_build, "runner": runner_build},
			"protocol": {"api_major": API_PROTOCOL_VERSION, "sidecar_major": SIDECAR_PROTOCOL_VERSION},
			"sqlite_schema_version": SQLITE_SCHEMA_VERSION,
			"playwright": {"version": playwright_version, "browser_entries": list(expected_entries)},
			"artifacts": _artifact_records(temporary),
		}
		(staged_sidecar / _MANIFEST_NAME).write_text(
			json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
		)
		verified = verify_bundle(temporary)
		temporary.replace(destination)
	except BaseException:
		shutil.rmtree(temporary, ignore_errors=True)
		raise
	return {**verified, "status": "staged"}


def build_bundle(
	*,
	pyinstaller_executable: Path | str | None,
	playwright_browsers_dir: Path | str,
	output_dir: Path | str,
	target: str,
	sidecar_build: str,
	runner_build: str,
) -> dict[str, Any]:
	"""Build a fresh PyInstaller ``onedir`` sidecar and stage it for Tauri."""

	_validate_native_linux_target(target)
	destination = Path(output_dir).expanduser().resolve()
	if destination.exists():
		raise ReleaseValidationError(f"release output already exists: {destination}")
	destination.parent.mkdir(parents=True, exist_ok=True)
	project_root = _project_root()
	with tempfile.TemporaryDirectory(prefix=f".{destination.name}.pyinstaller-", dir=destination.parent) as workspace:
		work_root = Path(workspace)
		dist_path = work_root / "dist"
		command = _pyinstaller_command(pyinstaller_executable)
		command.extend(
			[
				"--noconfirm",
				"--clean",
				"--onedir",
				"--name",
				_SIDECAR_RELATIVE_PATH.name,
				"--distpath",
				str(dist_path),
				"--workpath",
				str(work_root / "work"),
				"--specpath",
				str(work_root / "spec"),
				"--paths",
				str(project_root / "src"),
				"--collect-submodules",
				"browser_use",
				"--collect-data",
				"browser_use",
				"--collect-all",
				"playwright",
				"--collect-all",
				"tiktoken",
				"--collect-submodules",
				"tiktoken_ext",
				"--copy-metadata",
				"playwright",
				str(project_root / "src" / "browser_use" / "webretriever" / "desktop" / "sidecar.py"),
			]
		)
		completed = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
		if completed.returncode != 0:
			diagnostic = (completed.stderr or completed.stdout).strip()
			raise ReleaseValidationError(f"PyInstaller sidecar build failed: {diagnostic[-4_000:]}")
		result = stage_bundle(
			sidecar_dist=dist_path / _SIDECAR_RELATIVE_PATH.name,
			playwright_browsers_dir=playwright_browsers_dir,
			output_dir=destination,
			target=target,
			sidecar_build=sidecar_build,
			runner_build=runner_build,
		)
	return {**result, "status": "built"}


def install_tauri_resources(
	*, bundle_dir: Path | str, resources_dir: Path | str, replace: bool = False
) -> dict[str, Any]:
	"""Install one verified release tree into Tauri's fixed ``sidecar`` resource slot."""

	verified = verify_bundle(bundle_dir)
	source = Path(bundle_dir).expanduser().resolve() / "sidecar"
	resources = Path(resources_dir).expanduser().resolve()
	resources.mkdir(parents=True, exist_ok=True)
	destination = resources / "sidecar"
	if destination.exists() and not _replaceable_resource_slot(destination):
		if not replace:
			raise ReleaseValidationError(
				f"Tauri sidecar resource slot is occupied: {destination}; pass --replace to replace that slot"
			)
		if not destination.is_dir():
			raise ReleaseValidationError(f"Tauri sidecar resource slot is not a directory: {destination}")
	temporary = Path(tempfile.mkdtemp(prefix=".sidecar-install-", dir=resources))
	try:
		shutil.copytree(source, temporary / "sidecar", copy_function=shutil.copy2)
		verify_bundle(temporary)
		if destination.exists():
			shutil.rmtree(destination)
		(temporary / "sidecar").replace(destination)
	except BaseException:
		shutil.rmtree(temporary, ignore_errors=True)
		raise
	shutil.rmtree(temporary, ignore_errors=True)
	return {**verified, "status": "installed"}


def smoke_bundle(bundle_dir: Path | str) -> dict[str, Any]:
	"""Start the packaged sidecar with no Python/conda environment and verify readiness."""

	verified = verify_bundle(bundle_dir)
	root = Path(bundle_dir).expanduser().resolve()
	program = root.joinpath(*_SIDECAR_RELATIVE_PATH.parts)
	with tempfile.TemporaryDirectory(prefix="wr-release-smoke-") as state_dir:
		bearer_token = secrets.token_urlsafe(48)
		launch_nonce = secrets.token_urlsafe(24)
		environment = _sidecar_smoke_environment(
			bearer_token=bearer_token, launch_nonce=launch_nonce
		)
		process = subprocess.Popen(
			[str(program), "--state-dir", state_dir],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			env=environment,
		)
		try:
			base_url = _await_sidecar_handshake(process, launch_nonce)
			_await_ready(base_url, bearer_token)
			_request_shutdown(base_url, bearer_token)
			try:
				process.wait(timeout=10)
			except subprocess.TimeoutExpired as exc:
				raise ReleaseValidationError("smoke sidecar did not exit after authenticated shutdown") from exc
			if process.returncode != 0:
				raise ReleaseValidationError("smoke sidecar exited unsuccessfully")
		except BaseException:
			if process.poll() is None:
				process.terminate()
				try:
					process.wait(timeout=5)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait(timeout=5)
			raise
	return {**verified, "status": "smoke-passed"}


def verify_bundle(bundle_dir: Path | str, *, state_dir: Path | str | None = None) -> dict[str, Any]:
	"""Verify the public release-directory contract and return its build matrix."""

	root = Path(bundle_dir).expanduser().resolve()
	manifest = _read_manifest(root / "sidecar" / _MANIFEST_NAME)
	if manifest.get("format") != RELEASE_FORMAT:
		raise ReleaseValidationError(f"unsupported release manifest format: {manifest.get('format')!r}")
	if not isinstance(manifest.get("target"), str) or not manifest["target"].endswith("-unknown-linux-gnu"):
		raise ReleaseValidationError("release target must be a Linux GNU target triple")

	build = _mapping(manifest, "build")
	for name in ("app", "sidecar", "runner"):
		if not isinstance(build.get(name), str) or not build[name]:
			raise ReleaseValidationError(f"release build.{name} must be a non-empty string")
	if build["app"] != _desktop_app_build():
		raise ReleaseValidationError(
			f"desktop app build {build['app']!r} does not match the bundled Tauri build {_desktop_app_build()!r}"
		)

	protocol = _mapping(manifest, "protocol")
	if protocol.get("api_major") != API_PROTOCOL_VERSION:
		raise ReleaseValidationError(
			f"API protocol major {protocol.get('api_major')!r} is incompatible with desktop major {API_PROTOCOL_VERSION}"
		)
	if protocol.get("sidecar_major") != SIDECAR_PROTOCOL_VERSION:
		raise ReleaseValidationError(
			"sidecar protocol major "
			f"{protocol.get('sidecar_major')!r} is incompatible with desktop major {SIDECAR_PROTOCOL_VERSION}"
		)
	if manifest.get("sqlite_schema_version") != SQLITE_SCHEMA_VERSION:
		raise ReleaseValidationError(
			"SQLite schema version "
			f"{manifest.get('sqlite_schema_version')!r} is incompatible with release version {SQLITE_SCHEMA_VERSION}"
		)

	browser_entries = _browser_entries(manifest)
	artifacts = _artifacts(manifest)
	_verify_artifacts(root, artifacts)
	_verify_sidecar(root, artifacts)
	_verify_browser_entries(root, browser_entries, artifacts)
	_migrate_control_store(state_dir)
	return {
		"status": "verified",
		"target": manifest["target"],
		"app_build": build["app"],
		"sidecar_build": build["sidecar"],
		"api_protocol": protocol["api_major"],
		"sqlite_schema_version": manifest["sqlite_schema_version"],
	}


def _migrate_control_store(state_dir: Path | str | None) -> None:
	if state_dir is None:
		return
	state = Path(state_dir).expanduser().resolve()
	state.mkdir(parents=True, exist_ok=True)
	from browser_use.webretriever.desktop.store import SqliteControlStore

	store = SqliteControlStore(state / "control.sqlite3")
	store.close()


def _sidecar_smoke_environment(*, bearer_token: str, launch_nonce: str) -> dict[str, str]:
	"""Expose only the launch contract; a real release cannot inherit conda paths."""

	return {
		"PATH": os.defpath,
		"LANG": os.environ.get("LANG", "C.UTF-8"),
		"WR_SIDECAR_BEARER_TOKEN": bearer_token,
		"WR_SIDECAR_LAUNCH_NONCE": launch_nonce,
		"WR_SIDECAR_ALLOWED_ORIGINS": "tauri://localhost",
	}


def _await_sidecar_handshake(process: subprocess.Popen[str], launch_nonce: str) -> str:
	if process.stdout is None:
		raise ReleaseValidationError("smoke sidecar did not expose stdout")
	deadline = time.monotonic() + 15
	while time.monotonic() < deadline:
		if process.poll() is not None:
			raise ReleaseValidationError("smoke sidecar exited before its bootstrap handshake")
		remaining = deadline - time.monotonic()
		readable, _, _ = select.select([process.stdout], [], [], max(remaining, 0))
		if not readable:
			break
		line = process.stdout.readline()
		if not line:
			continue
		if not line.startswith("WR_SIDECAR_LISTENING "):
			continue
		try:
			payload = json.loads(line.removeprefix("WR_SIDECAR_LISTENING "))
		except json.JSONDecodeError as exc:
			raise ReleaseValidationError("smoke sidecar emitted an invalid bootstrap handshake") from exc
		if not isinstance(payload, dict):
			raise ReleaseValidationError("smoke sidecar emitted an invalid bootstrap handshake")
		if payload.get("protocol_version") != SIDECAR_PROTOCOL_VERSION:
			raise ReleaseValidationError("smoke sidecar protocol major is incompatible")
		if payload.get("launch_nonce") != launch_nonce:
			raise ReleaseValidationError("smoke sidecar launch nonce did not match")
		port = payload.get("port")
		if not isinstance(port, int) or not 1 <= port <= 65535:
			raise ReleaseValidationError("smoke sidecar bootstrap port is invalid")
		if payload.get("pid") != process.pid:
			raise ReleaseValidationError("smoke sidecar bootstrap PID did not match")
		return f"http://127.0.0.1:{port}"
	raise ReleaseValidationError("smoke sidecar did not emit a bootstrap handshake before timeout")


def _await_ready(base_url: str, bearer_token: str) -> None:
	deadline = time.monotonic() + 15
	request = Request(base_url + "/api/v1/health/ready", headers={"Authorization": f"Bearer {bearer_token}"})
	while time.monotonic() < deadline:
		try:
			with urlopen(request, timeout=2) as response:  # noqa: S310 -- loopback URL comes from verified handshake
				payload = json.loads(response.read().decode("utf-8"))
		except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
			time.sleep(0.1)
			continue
		if not isinstance(payload, dict) or payload.get("status") != "READY":
			raise ReleaseValidationError("smoke sidecar returned an invalid readiness payload")
		if payload.get("api_protocol") != API_PROTOCOL_VERSION:
			raise ReleaseValidationError("smoke sidecar API protocol major is incompatible")
		return
	raise ReleaseValidationError("smoke sidecar did not become ready before timeout")


def _request_shutdown(base_url: str, bearer_token: str) -> None:
	request = Request(
		base_url + "/api/v1/control/shutdown",
		data=b"",
		headers={"Authorization": f"Bearer {bearer_token}"},
		method="POST",
	)
	try:
		with urlopen(request, timeout=5) as response:  # noqa: S310 -- loopback URL comes from verified handshake
			if response.status != 202:
				raise ReleaseValidationError("smoke sidecar rejected authenticated shutdown")
	except (HTTPError, URLError, TimeoutError) as exc:
		raise ReleaseValidationError("smoke sidecar did not accept authenticated shutdown") from exc


def _read_manifest(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError as exc:
		raise ReleaseValidationError(f"release manifest is missing: {path}") from exc
	except json.JSONDecodeError as exc:
		raise ReleaseValidationError(f"release manifest is not valid JSON: {exc}") from exc
	if not isinstance(value, dict):
		raise ReleaseValidationError("release manifest must be a JSON object")
	return value


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
	mapping = value.get(name)
	if not isinstance(mapping, dict):
		raise ReleaseValidationError(f"release manifest {name} must be an object")
	return mapping


def _browser_entries(manifest: dict[str, Any]) -> tuple[str, ...]:
	playwright = _mapping(manifest, "playwright")
	if not isinstance(playwright.get("version"), str) or not playwright["version"]:
		raise ReleaseValidationError("release manifest playwright.version must be a non-empty string")
	entries = playwright.get("browser_entries")
	if not isinstance(entries, list) or not entries or any(not isinstance(entry, str) or not entry for entry in entries):
		raise ReleaseValidationError("release manifest playwright.browser_entries must be a non-empty string list")
	if len(entries) != len(set(entries)):
		raise ReleaseValidationError("release manifest playwright.browser_entries must not contain duplicates")
	return tuple(entries)


def _artifacts(manifest: dict[str, Any]) -> dict[PurePosixPath, dict[str, Any]]:
	records = manifest.get("artifacts")
	if not isinstance(records, list) or not records:
		raise ReleaseValidationError("release manifest artifacts must be a non-empty list")
	artifacts: dict[PurePosixPath, dict[str, Any]] = {}
	for record in records:
		if not isinstance(record, dict):
			raise ReleaseValidationError("release manifest artifact records must be objects")
		raw_path = record.get("path")
		sha256 = record.get("sha256")
		if not isinstance(raw_path, str) or not isinstance(sha256, str) or len(sha256) != 64:
			raise ReleaseValidationError("each release artifact needs a relative path and SHA-256 digest")
		path = PurePosixPath(raw_path)
		if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
			raise ReleaseValidationError(f"release artifact path is unsafe: {raw_path!r}")
		if path in artifacts:
			raise ReleaseValidationError(f"release manifest repeats artifact {raw_path!r}")
		if not isinstance(record.get("executable"), bool):
			raise ReleaseValidationError(f"release artifact {raw_path!r} must declare executable")
		artifacts[path] = record
	return artifacts


def _verify_artifacts(root: Path, artifacts: dict[PurePosixPath, dict[str, Any]]) -> None:
	actual = {
		PurePosixPath(path.relative_to(root).as_posix())
		for path in root.rglob("*")
		if path.is_file() and path.relative_to(root).as_posix() != f"sidecar/{_MANIFEST_NAME}"
	}
	expected = set(artifacts)
	if actual != expected:
		missing = sorted(str(path) for path in expected - actual)
		unexpected = sorted(str(path) for path in actual - expected)
		raise ReleaseValidationError(f"release payload differs from manifest (missing={missing}, unexpected={unexpected})")
	for relative_path, record in artifacts.items():
		path = root.joinpath(*relative_path.parts)
		if _sha256(path) != record["sha256"]:
			raise ReleaseValidationError(f"release artifact digest mismatch: {relative_path}")
		if bool(path.stat().st_mode & 0o111) != record["executable"]:
			raise ReleaseValidationError(f"release artifact executable mode mismatch: {relative_path}")


def _verify_sidecar(root: Path, artifacts: dict[PurePosixPath, dict[str, Any]]) -> None:
	record = artifacts.get(_SIDECAR_RELATIVE_PATH)
	if record is None or not record["executable"]:
		raise ReleaseValidationError("release must contain an executable sidecar/webretriever-sidecar")
	if not os.access(root.joinpath(*_SIDECAR_RELATIVE_PATH.parts), os.X_OK):
		raise ReleaseValidationError("release sidecar is not executable")


def _verify_browser_entries(
	root: Path, browser_entries: tuple[str, ...], artifacts: dict[PurePosixPath, dict[str, Any]]
) -> None:
	for entry in browser_entries:
		directory = root.joinpath(*_BROWSER_ROOT.parts, entry)
		if not directory.is_dir():
			raise ReleaseValidationError(f"release is missing Playwright browser entry {entry!r}")
		prefix = _BROWSER_ROOT / entry
		if not any(path.is_relative_to(prefix) for path in artifacts):
			raise ReleaseValidationError(f"Playwright browser entry {entry!r} has no verified files")


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _artifact_records(root: Path) -> list[dict[str, Any]]:
	return [
		{
			"path": path.relative_to(root).as_posix(),
			"sha256": _sha256(path),
			"executable": bool(path.stat().st_mode & 0o111),
		}
		for path in sorted(root.rglob("*"))
		if path.is_file() and path.relative_to(root).as_posix() != f"sidecar/{_MANIFEST_NAME}"
	]


def _replaceable_resource_slot(path: Path) -> bool:
	return path.is_dir() and {entry.name for entry in path.iterdir()} <= {".gitkeep"}


def _expected_playwright_browser_entries() -> tuple[tuple[str, ...], str]:
	"""Read the native browser revisions from the installed pinned Playwright SDK."""

	try:
		package = import_module("playwright")
		browsers_path = Path(package.__file__).resolve().parent / "driver" / "package" / "browsers.json"
		browsers = json.loads(browsers_path.read_text(encoding="utf-8"))["browsers"]
		playwright_version = metadata.version("playwright")
	except (ImportError, OSError, KeyError, TypeError, json.JSONDecodeError, metadata.PackageNotFoundError) as exc:
		raise ReleaseValidationError("the pinned Playwright SDK is required to stage a release") from exc
	by_name = {item.get("name"): item for item in browsers if isinstance(item, dict)}
	entries: list[str] = []
	for name in ("chromium", "chromium-headless-shell", "ffmpeg"):
		entry = by_name.get(name)
		revision = entry.get("revision") if isinstance(entry, dict) else None
		if not isinstance(revision, str) or not revision:
			raise ReleaseValidationError(f"installed Playwright does not declare {name!r}")
		entries.append(f"{name.replace('-', '_')}-{revision}")
	return tuple(entries), playwright_version


def _desktop_app_build() -> str:
	"""Read the release authority shared by the Tauri package and its sidecar."""

	config_path = _project_root() / "desktop" / "src-tauri" / "tauri.conf.json"
	try:
		value = json.loads(config_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise ReleaseValidationError(f"cannot read Tauri release version: {config_path}") from exc
	version = value.get("version") if isinstance(value, dict) else None
	if not isinstance(version, str) or not version:
		raise ReleaseValidationError("Tauri release version must be a non-empty string")
	return version


def _project_root() -> Path:
	return Path(__file__).resolve().parents[4]


def _validate_native_linux_target(target: str) -> None:
	architectures = {
		"x86_64": "x86_64-unknown-linux-gnu",
		"amd64": "x86_64-unknown-linux-gnu",
		"aarch64": "aarch64-unknown-linux-gnu",
		"arm64": "aarch64-unknown-linux-gnu",
	}
	if sys.platform != "linux" or platform.machine().lower() not in architectures:
		raise ReleaseValidationError("release staging requires a supported native Linux target host")
	expected = architectures[platform.machine().lower()]
	if target != expected:
		raise ReleaseValidationError(
			f"release staging requires native Linux target {expected!r}, not {target!r}"
		)


def _pyinstaller_command(pyinstaller_executable: Path | str | None) -> list[str]:
	if pyinstaller_executable is None:
		return [sys.executable, "-m", "PyInstaller"]
	path = Path(pyinstaller_executable).expanduser().resolve()
	if not path.is_file() or not os.access(path, os.X_OK):
		raise ReleaseValidationError(f"PyInstaller executable is not executable: {path}")
	return [str(path)]


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Build and verify self-contained WebRetriever desktop releases.")
	subcommands = parser.add_subparsers(dest="command", required=True)
	verify = subcommands.add_parser("verify", help="verify a Linux release directory")
	verify.add_argument("--bundle-dir", required=True, type=Path)
	verify.add_argument("--state-dir", type=Path, help="migrate and verify this sidecar control-state directory")
	stage = subcommands.add_parser("stage", help="stage a PyInstaller sidecar and its Playwright browser payload")
	stage.add_argument("--sidecar-dist", required=True, type=Path)
	stage.add_argument("--playwright-browsers-dir", required=True, type=Path)
	stage.add_argument("--output-dir", required=True, type=Path)
	stage.add_argument("--target", required=True)
	stage.add_argument("--sidecar-build", required=True)
	stage.add_argument("--runner-build", required=True)
	build = subcommands.add_parser("build", help="build a fresh PyInstaller onedir sidecar and stage a release")
	build.add_argument("--pyinstaller-executable", type=Path)
	build.add_argument("--playwright-browsers-dir", required=True, type=Path)
	build.add_argument("--output-dir", required=True, type=Path)
	build.add_argument("--target", required=True)
	build.add_argument("--sidecar-build", required=True)
	build.add_argument("--runner-build", required=True)
	install = subcommands.add_parser("install-tauri-resources", help="install a verified sidecar into Tauri resources")
	install.add_argument("--bundle-dir", required=True, type=Path)
	install.add_argument("--resources-dir", required=True, type=Path)
	install.add_argument("--replace", action="store_true", help="replace an existing managed sidecar resource slot")
	smoke = subcommands.add_parser("smoke", help="start and stop the packaged sidecar without conda")
	smoke.add_argument("--bundle-dir", required=True, type=Path)
	return parser


def main(argv: Sequence[str] | None = None) -> int:
	args = _parser().parse_args(argv)
	try:
		if args.command == "verify":
			result = verify_bundle(args.bundle_dir, state_dir=args.state_dir)
		elif args.command == "stage":
			result = stage_bundle(
				sidecar_dist=args.sidecar_dist,
				playwright_browsers_dir=args.playwright_browsers_dir,
				output_dir=args.output_dir,
				target=args.target,
				sidecar_build=args.sidecar_build,
				runner_build=args.runner_build,
			)
		elif args.command == "build":
			result = build_bundle(
				pyinstaller_executable=args.pyinstaller_executable,
				playwright_browsers_dir=args.playwright_browsers_dir,
				output_dir=args.output_dir,
				target=args.target,
				sidecar_build=args.sidecar_build,
				runner_build=args.runner_build,
			)
		elif args.command == "install-tauri-resources":
			result = install_tauri_resources(
				bundle_dir=args.bundle_dir, resources_dir=args.resources_dir, replace=args.replace
			)
		else:
			result = smoke_bundle(args.bundle_dir)
	except ReleaseValidationError as exc:
		print(f"release verification failed: {exc}", file=os.sys.stderr)
		return 2
	print(json.dumps(result, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
