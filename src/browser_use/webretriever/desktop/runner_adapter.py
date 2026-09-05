"""Sidecar-only mapping from the public RunSpec to the existing Runner."""

from __future__ import annotations

import os
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from browser_use.webretriever.desktop.contracts import PreflightResult, RunEventDraft, RunSpec
from browser_use.webretriever.desktop.telemetry import RunTelemetry
from browser_use.webretriever.model_services import ModelServiceConfig
from browser_use.webretriever.models import load_tasks
from browser_use.webretriever.run_control import CancellationToken
from browser_use.webretriever.runner import RunnerConfig, run as execute_runner


class PreflightError(ValueError):
	"""A safe, user-actionable failure at the side-effect-free boundary."""


@dataclass(frozen=True, slots=True)
class RunnerProfile:
	"""Server-side credential material resolved from an opaque profile id."""

	model: str
	model_services: tuple[ModelServiceConfig, ...]
	api_mode: str = "auto"
	reasoning_effort: str = "medium"


class ProfileResolver(Protocol):
	def resolve(self, profile_id: str) -> RunnerProfile: ...


class StaticProfileResolver:
	"""Small injectable profile registry for the sidecar process and tests."""

	def __init__(self, profiles: Mapping[str, RunnerProfile]) -> None:
		self._profiles = dict(profiles)

	def resolve(self, profile_id: str) -> RunnerProfile:
		try:
			return self._profiles[profile_id]
		except KeyError as exc:
			raise PreflightError("the selected model credential profile is unavailable") from exc


class JsonProfileResolver:
	"""Resolve a profile id to a sidecar-local config file without HTTP secrets."""

	def __init__(self, config_paths: Mapping[str, str | Path]) -> None:
		self._config_paths = {profile_id: Path(path) for profile_id, path in config_paths.items()}

	def resolve(self, profile_id: str) -> RunnerProfile:
		try:
			path = self._config_paths[profile_id].expanduser().resolve(strict=True)
			with path.open(encoding="utf-8") as file:
				payload = json.load(file)
		except (KeyError, OSError, json.JSONDecodeError) as exc:
			raise PreflightError("the selected model credential profile is unavailable") from exc
		if not isinstance(payload, Mapping):
			raise PreflightError("the selected model credential profile is invalid")
		model = payload.get("api_model")
		raw_services = payload.get("model_services")
		if not isinstance(model, str) or not model.strip() or not isinstance(raw_services, list) or not raw_services:
			raise PreflightError("the selected model credential profile is invalid")
		services: list[ModelServiceConfig] = []
		for raw in raw_services:
			if not isinstance(raw, Mapping):
				raise PreflightError("the selected model credential profile is invalid")
			name, api_base, api_key = raw.get("name"), raw.get("api_base"), raw.get("api_key")
			if not all(isinstance(value, str) and value.strip() for value in (name, api_base, api_key)):
				raise PreflightError("the selected model credential profile is invalid")
			services.append(ModelServiceConfig(name.strip(), api_base.strip(), api_key.strip()))
		api_mode = payload.get("api_mode", "auto")
		reasoning_effort = payload.get("reasoning_effort", "medium")
		if api_mode not in {"auto", "responses", "chat-completions"} or reasoning_effort not in {"low", "medium", "high"}:
			raise PreflightError("the selected model credential profile is invalid")
		return RunnerProfile(
			model=model.strip(),
			model_services=tuple(services),
			api_mode=api_mode,
			reasoning_effort=reasoning_effort,
		)


class RunnerAdapter:
	"""Keep FastAPI and profile secrets out of the Runner/Agent core."""

	def __init__(
		self,
		*,
		profiles: ProfileResolver,
		runner: Callable[..., Awaitable[dict[str, Any]]] = execute_runner,
	) -> None:
		self._profiles = profiles
		self._runner = runner

	async def preflight(self, spec: RunSpec) -> PreflightResult:
		"""Validate files, selected work, credentials and Runner limits without execution."""

		config = self._config_for(spec, output_dir=None)
		try:
			config.validate()
		except ValueError as exc:
			raise PreflightError(str(exc)) from exc
		try:
			tasks = load_tasks(config.input_path)
		except (OSError, ValueError) as exc:
			raise PreflightError("the task input is not a valid WebRetriever task file") from exc
		selected = tasks
		if config.task_indices is not None:
			selected = [task for task in selected if task.task_idx in config.task_indices]
		if config.limit is not None:
			selected = selected[: config.limit]
		if not selected:
			raise PreflightError("the selected task subset is empty")
		return PreflightResult(task_count=len(selected))

	async def run(
		self,
		spec: RunSpec,
		emit: Callable[[RunEventDraft], Awaitable[None]],
		*,
		cancellation: CancellationToken,
		output_dir: Path,
	) -> dict[str, Any]:
		config = self._config_for(spec, output_dir=output_dir)
		return await self._runner(config, observer=RunTelemetry(emit), cancellation=cancellation)

	def _config_for(self, spec: RunSpec, *, output_dir: Path | None) -> RunnerConfig:
		input_path = _regular_file(spec.input_path, label="task input")
		output_root = _writable_directory(spec.output_root)
		profile = self._profiles.resolve(spec.model.profile_id)
		if not profile.model.strip() or not profile.model_services:
			raise PreflightError("the selected model credential profile is invalid")
		# The output directory itself is created only after RunManager has accepted
		# the run.  Preflight therefore checks the selected root, not a future path.
		actual_output = output_dir if output_dir is not None else output_root / ".preflight-does-not-write"
		return RunnerConfig(
			input_path=input_path,
			output_dir=actual_output,
			model=profile.model,
			cdp_urls=[],
			model_services=list(profile.model_services),
			api_mode=profile.api_mode,  # type: ignore[arg-type]
			max_steps=spec.limits.max_steps,
			model_timeout_seconds=spec.limits.model_timeout_seconds,
			task_timeout_seconds=spec.limits.task_timeout_seconds,
			max_concurrency=spec.limits.max_concurrency,
			reasoning_effort=profile.reasoning_effort,  # type: ignore[arg-type]
			local_browser=True,
			headless=not spec.browser.headed,
			rerun_failed=False,
			task_indices=frozenset(spec.selection.task_indices) if spec.selection.task_indices is not None else None,
			limit=spec.selection.limit,
		)


def _regular_file(value: str, *, label: str) -> Path:
	try:
		path = Path(value).expanduser().resolve(strict=True)
	except OSError as exc:
		raise PreflightError(f"{label} does not exist or cannot be resolved") from exc
	if not path.is_file():
		raise PreflightError(f"{label} must be a regular file")
	if not os.access(path, os.R_OK):
		raise PreflightError(f"{label} is not readable")
	return path


def _writable_directory(value: str) -> Path:
	try:
		path = Path(value).expanduser().resolve(strict=True)
	except OSError as exc:
		raise PreflightError("output root does not exist or cannot be resolved") from exc
	if not path.is_dir():
		raise PreflightError("output root must be a directory")
	if not os.access(path, os.W_OK | os.X_OK):
		raise PreflightError("output root is not writable")
	return path


__all__ = [
	"JsonProfileResolver",
	"PreflightError",
	"ProfileResolver",
	"RunnerAdapter",
	"RunnerProfile",
	"StaticProfileResolver",
]
