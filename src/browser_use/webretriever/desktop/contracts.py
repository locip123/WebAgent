"""Public v1 data-transfer contracts for the local desktop control plane."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from browser_use.webretriever.runner import (
	DEFAULT_MAX_CONCURRENCY,
	DEFAULT_TASK_TIMEOUT_SECONDS,
	MAX_CONCURRENCY,
)
from browser_use.webretriever.desktop.versioning import API_PROTOCOL_VERSION
from browser_use.webretriever.model_services import (
	DEFAULT_MODEL_SERVICE_MODEL,
	DEFAULT_MODEL_SERVICE_NAME,
	DEFAULT_MODEL_SERVICE_RESPONSE_MODE,
)

SCHEMA_VERSION = API_PROTOCOL_VERSION
MAX_STEPS = 100
MAX_MODEL_TIMEOUT_SECONDS = 180


class _ContractModel(BaseModel):
	"""Strict wire-model defaults shared by v1 DTOs."""

	model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


_PROBLEM_TEMPLATES: dict[str, tuple[str, int, str]] = {
	"unauthorized": (
		"Unauthorized",
		401,
		"A valid sidecar launch bearer token is required.",
	),
	"validation_failed": (
		"Validation failed",
		422,
		"The request does not satisfy the local control-plane contract.",
	),
	"run_not_found": (
		"Run not found",
		404,
		"No local run matches this run_id.",
	),
	"artifact_not_found": (
		"Artifact not found",
		404,
		"No artifact with this identifier belongs to the run.",
	),
	"sidecar_draining": (
		"Sidecar is shutting down",
		503,
		"The sidecar is draining and cannot accept a new run.",
	),
	"active_run_exists": (
		"Another run is active",
		409,
		"Wait for or cancel the active run before starting another run.",
	),
	"idempotency_key_reused": (
		"Idempotency key was reused",
		409,
		"Use a new Idempotency-Key for a different run request.",
	),
	"events_expired": (
		"Requested events have expired",
		410,
		"Reload the run snapshot and resume from its event cursor.",
	),
	"project_not_found": (
		"Project not found",
		404,
		"No local project matches this project_id.",
	),
	"project_has_active_run": (
		"Project has an active run",
		409,
		"Wait for or cancel the active project run before deleting the project.",
	),
	"project_url_mismatch": (
		"Project URL mismatch",
		409,
		"The project_id is already owned by a different website URL.",
	),
	"project_path_unsafe": (
		"Project storage path is unsafe",
		409,
		"The project references a path outside the sidecar-managed state directory.",
	),
	"project_files_in_use": (
		"Project files are in use",
		409,
		"The project could not be deleted because a local resource is unavailable.",
	),
	"project_delete_failed": (
		"Project deletion failed",
		500,
		"The sidecar could not remove the project's local files and records.",
	),
}


class ProblemDetails(_ContractModel):
	"""Sanitized RFC 9457-compatible error response for the public API."""

	type: str = Field(min_length=1, max_length=256)
	title: str = Field(min_length=1, max_length=256)
	status: int = Field(ge=400, le=599)
	detail: str = Field(min_length=1, max_length=2_000)
	instance: str = Field(min_length=1, max_length=4_096)
	error_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
	trace_id: str | None = Field(default=None, min_length=1, max_length=128)
	run_id: str | None = Field(default=None, min_length=1, max_length=128)
	errors: dict[str, list[str]] | None = None

	@classmethod
	def for_error(
		cls,
		*,
		code: str,
		status: int,
		instance: str,
		trace_id: str | None = None,
		run_id: str | None = None,
		errors: dict[str, list[str]] | None = None,
	) -> ProblemDetails:
		"""Create a known, sanitized problem response from its stable error code."""

		title, expected_status, detail = _PROBLEM_TEMPLATES[code]
		if status != expected_status:
			raise ValueError(f"{code} must use HTTP {expected_status}")
		return cls(
			type="urn:webretriever:problem:" + code.replace("_", "-"),
			title=title,
			status=status,
			detail=detail,
			instance=instance,
			error_code=code,
			trace_id=trace_id,
			run_id=run_id,
			errors=errors,
		)


class AccountProfile(_ContractModel):
	"""The locally stored profile for the desktop application's single user."""

	name: str = Field(default="林晓宇", max_length=100)
	email: str = Field(default="", max_length=254)
	age: int | None = Field(default=None, ge=0, le=150)
	work: str = Field(default="", max_length=100)
	organization: str = Field(default="", max_length=100)

	@field_validator("email")
	@classmethod
	def _email_is_valid(cls, value: str) -> str:
		if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
			raise ValueError("email must be a valid email address")
		return value


ModelServiceResponseMode = Literal["responses", "chat-completions"]


class ModelServiceSummary(_ContractModel):
	"""A configured service excluding its credential."""

	name: str = Field(default=DEFAULT_MODEL_SERVICE_NAME, min_length=1, max_length=100)
	api_base: str = Field(min_length=1, max_length=4_096)
	model: str = Field(default=DEFAULT_MODEL_SERVICE_MODEL, min_length=1, max_length=256)
	response_mode: ModelServiceResponseMode = DEFAULT_MODEL_SERVICE_RESPONSE_MODE

	@field_validator("api_base")
	@classmethod
	def _api_base_is_valid(cls, value: str) -> str:
		parts = urlsplit(value)
		if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
			raise ValueError("api_base must be an absolute HTTP(S) URL without credentials")
		return value.rstrip("/")


class ModelServiceInput(ModelServiceSummary):
	"""A service supplied to the local sidecar, including its write-only credential."""

	api_key: str = Field(min_length=1, max_length=4_096)


class ModelServiceTestResult(_ContractModel):
	name: str = Field(min_length=1, max_length=100)
	success: bool
	error_code: str | None = None


EventType = Literal[
	"run.accepted",
	"run.started",
	"worker.state_changed",
	"task.started",
	"task.phase_changed",
	"task.step.decided",
	"task.step.completed",
	"task.recovery",
	"artifact.available",
	"task.finished",
	"run.cancel_requested",
	"run.cancelled",
	"run.completed",
	"run.failed",
	"run.interrupted",
	"telemetry.warning",
]
EventLevel = Literal["debug", "info", "warning", "error"]


class EventTaskReference(_ContractModel):
	"""The stable task identity carried by task-scoped events."""

	task_id: str = Field(min_length=1, max_length=128)
	task_idx: int = Field(ge=0)


class RunEvent(_ContractModel):
	"""A durable, per-run event that can be replayed by its cursor."""

	model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, serialize_by_alias=True)

	event_schema: Literal["webretriever.run-event/v1"] = Field(
		default="webretriever.run-event/v1", alias="schema", serialization_alias="schema"
	)
	run_id: str = Field(min_length=1, max_length=128)
	event_id: int = Field(ge=1)
	type: EventType
	occurred_at: datetime
	level: EventLevel
	task: EventTaskReference | None = None
	payload: dict[str, Any] = Field(default_factory=dict)

	@model_validator(mode="after")
	def _occurred_at_is_utc(self) -> RunEvent:
		if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timezone.utc.utcoffset(self.occurred_at):
			raise ValueError("occurred_at must use UTC")
		return self


class RunEventDraft(_ContractModel):
	"""An event before the run journal assigns its durable cursor and timestamp."""

	type: EventType
	level: EventLevel
	task: EventTaskReference | None = None
	payload: dict[str, Any] = Field(default_factory=dict)


RunStatus = Literal["STARTING", "RUNNING", "CANCELLING", "COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"]


class RunAccepted(_ContractModel):
	"""The asynchronous acceptance response returned immediately after creation."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	run_id: str = Field(min_length=1, max_length=128)
	status: Literal["STARTING"] = "STARTING"
	created_at: datetime
	snapshot_url: str = Field(min_length=1, max_length=4_096)
	events_url: str = Field(min_length=1, max_length=4_096)


class RunSnapshot(_ContractModel):
	"""The authoritative control-plane projection for one desktop run."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	run_id: str = Field(min_length=1, max_length=128)
	project_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
	project_url: str | None = Field(default=None, min_length=1, max_length=4_096)
	status: RunStatus
	created_at: datetime
	started_at: datetime | None = None
	finished_at: datetime | None = None
	output_dir: str = Field(min_length=1, max_length=4_096)
	last_event_id: int = Field(ge=0)
	summary: dict[str, Any] | None = None
	error: dict[str, Any] | None = None


class ReadyInfo(_ContractModel):
	"""Non-sensitive readiness details for the desktop supervisor."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	sidecar_build: str = Field(min_length=1, max_length=128)
	api_protocol: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	runner_build: str = Field(min_length=1, max_length=128)
	pid: int = Field(ge=1)
	status: Literal["READY"] = "READY"


class RuntimeCapabilities(_ContractModel):
	"""The intentionally narrow set of desktop-v1 controls."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	browser_modes: list[Literal["local"]] = Field(default_factory=lambda: ["local"])
	limits: "RunnerLimits" = Field(default_factory=lambda: RunnerLimits())


class PreflightResult(_ContractModel):
	"""Side-effect-free validation and selected task summary."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	task_count: int = Field(ge=1, le=100)
	warnings: list[str] = Field(default_factory=list, max_length=100)


class RunPage(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	items: list[RunSnapshot] = Field(default_factory=list, max_length=100)
	next_cursor: str | None = Field(default=None, min_length=1, max_length=256)


class ProjectRegistration(_ContractModel):
	"""The URL ownership assertion for a client-generated project identifier."""

	website_url: str = Field(min_length=1, max_length=4_096)

	@field_validator("website_url")
	@classmethod
	def _website_url_is_valid(cls, value: str) -> str:
		if any(character.isspace() or ord(character) < 32 for character in value):
			raise ValueError("website_url must not contain whitespace or control characters")
		parts = urlsplit(value)
		if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.hostname is None:
			raise ValueError("website_url must be an absolute HTTP(S) URL")
		if parts.username is not None or parts.password is not None:
			raise ValueError("website_url must not contain credentials")
		try:
			_ = parts.port
		except ValueError as exc:
			raise ValueError("website_url contains an invalid port") from exc
		return value


class ProjectSummary(_ContractModel):
	"""A sidecar-owned project identity and its website URL."""

	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	project_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
	website_url: str = Field(min_length=1, max_length=4_096)
	created_at: datetime


class ProjectPage(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	items: list[ProjectSummary] = Field(default_factory=list, max_length=100)
	next_cursor: str | None = Field(default=None, min_length=1, max_length=256)


class ProjectHistoryItem(RunSnapshot):
	"""One project run together with its retained interaction journal."""

	instruction: str | None = Field(default=None, max_length=20_000)
	events: list[RunEvent] = Field(default_factory=list, max_length=10_000)


class ProjectHistoryPage(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	items: list[ProjectHistoryItem] = Field(default_factory=list, max_length=100)
	next_cursor: str | None = Field(default=None, min_length=1, max_length=256)


class CancelRequest(_ContractModel):
	reason: str | None = Field(default=None, min_length=1, max_length=500)


class CancelResult(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	run_id: str = Field(min_length=1, max_length=128)
	status: RunStatus
	cancel_applied: bool


class ShutdownAccepted(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	status: Literal["DRAINING"] = "DRAINING"


class Artifact(_ContractModel):
	"""A sidecar-indexed local artifact; paths never cross the HTTP boundary."""

	artifact_id: str = Field(min_length=1, max_length=128)
	kind: str = Field(min_length=1, max_length=128)
	task_id: str | None = Field(default=None, min_length=1, max_length=128)
	mime_type: str = Field(min_length=1, max_length=128)
	size: int = Field(ge=0)


class ArtifactPage(_ContractModel):
	schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
	items: list[Artifact] = Field(default_factory=list, max_length=100)
	next_cursor: str | None = Field(default=None, min_length=1, max_length=256)


class CredentialProfileReference(_ContractModel):
	"""A sidecar-owned reference to a locally stored model credential."""

	profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LocalBrowserSpec(_ContractModel):
	"""The only browser mode exposed by desktop v1."""

	mode: Literal["local"]
	headed: bool = False


class TaskSubmissionRequest(_ContractModel):
	"""A single workspace task submitted from the desktop home screen."""

	task: str = Field(min_length=1, max_length=20_000)
	website_url: str = Field(min_length=1, max_length=4_096)
	project_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

	@field_validator("website_url")
	@classmethod
	def _website_url_is_valid(cls, value: str) -> str:
		if any(character.isspace() or ord(character) < 32 for character in value):
			raise ValueError("website_url must not contain whitespace or control characters")
		parts = urlsplit(value)
		if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.hostname is None:
			raise ValueError("website_url must be an absolute HTTP(S) URL")
		if parts.username is not None or parts.password is not None:
			raise ValueError("website_url must not contain credentials")
		try:
			_ = parts.port
		except ValueError as exc:
			raise ValueError("website_url contains an invalid port") from exc
		return value


class RunnerLimits(_ContractModel):
	"""The bounded Runner controls deliberately exposed to desktop users."""

	max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=MAX_CONCURRENCY)
	max_steps: int = Field(default=MAX_STEPS, ge=1, le=MAX_STEPS)
	model_timeout_seconds: float = Field(default=float(MAX_MODEL_TIMEOUT_SECONDS), gt=0, le=MAX_MODEL_TIMEOUT_SECONDS)
	task_timeout_seconds: float = Field(default=DEFAULT_TASK_TIMEOUT_SECONDS, gt=0)


class TaskSelection(_ContractModel):
	"""An optional, bounded subset of the validated input task list."""

	task_indices: list[int] | None = Field(default=None, max_length=100)
	limit: int | None = Field(default=None, ge=1, le=100)

	@field_validator("task_indices")
	@classmethod
	def _task_indices_are_unique_and_non_negative(cls, value: list[int] | None) -> list[int] | None:
		if value is None:
			return None
		if any(index < 0 for index in value):
			raise ValueError("task_indices must contain only non-negative values")
		if len(value) != len(set(value)):
			raise ValueError("task_indices must not contain duplicates")
		return value


class RunSpec(_ContractModel):
	"""The complete public request for one desktop run.

	Its paths are strings on the wire.  Filesystem access and canonicalization are
	intentionally deferred to the side-effect-free preflight boundary.
	"""

	schema_version: Literal[SCHEMA_VERSION]
	input_path: str = Field(min_length=1, max_length=4_096)
	output_root: str = Field(min_length=1, max_length=4_096)
	project_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
	project_url: str | None = Field(default=None, min_length=1, max_length=4_096)
	model: CredentialProfileReference
	browser: LocalBrowserSpec
	limits: RunnerLimits = Field(default_factory=RunnerLimits)
	selection: TaskSelection = Field(default_factory=TaskSelection)

	@field_validator("project_url")
	@classmethod
	def _project_url_is_valid(cls, value: str | None) -> str | None:
		if value is None:
			return None
		if any(character.isspace() or ord(character) < 32 for character in value):
			raise ValueError("project_url must not contain whitespace or control characters")
		parts = urlsplit(value)
		if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.hostname is None:
			raise ValueError("project_url must be an absolute HTTP(S) URL")
		if parts.username is not None or parts.password is not None:
			raise ValueError("project_url must not contain credentials")
		try:
			_ = parts.port
		except ValueError as exc:
			raise ValueError("project_url contains an invalid port") from exc
		return value

	@model_validator(mode="after")
	def _project_id_requires_url(self) -> RunSpec:
		if self.project_id is not None and self.project_url is None:
			raise ValueError("project_id requires project_url")
		return self


__all__ = [
	"AccountProfile",
	"ModelServiceInput",
	"ModelServiceSummary",
	"ModelServiceTestResult",
	"CredentialProfileReference",
	"Artifact",
	"ArtifactPage",
	"CancelRequest",
	"CancelResult",
	"LocalBrowserSpec",
	"MAX_MODEL_TIMEOUT_SECONDS",
	"MAX_STEPS",
	"EventLevel",
	"EventTaskReference",
	"EventType",
	"ProblemDetails",
	"PreflightResult",
	"ReadyInfo",
	"RunAccepted",
	"RunEvent",
	"RunEventDraft",
	"RunnerLimits",
	"RunSpec",
	"RunSnapshot",
	"RunPage",
	"ProjectHistoryItem",
	"ProjectHistoryPage",
	"ProjectPage",
	"ProjectRegistration",
	"ProjectSummary",
	"RunStatus",
	"SCHEMA_VERSION",
	"ShutdownAccepted",
	"TaskSelection",
	"RuntimeCapabilities",
	"TaskSubmissionRequest",
]
