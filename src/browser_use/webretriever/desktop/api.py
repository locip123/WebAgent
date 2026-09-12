"""Authenticated FastAPI boundary for the local desktop control plane.

The application factory deliberately receives its RunManager.  This keeps HTTP
tests deterministic and prevents FastAPI types from leaking into Runner code.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

from browser_use.webretriever.desktop.account_profile_store import AccountProfileStore
from browser_use.webretriever.desktop.model_service_store import ModelServiceStore
from browser_use.webretriever.desktop.model_service_connection import (
	ModelServiceConnectionError,
	test_model_service_connection,
)
from browser_use.webretriever.desktop.contracts import (
	AccountProfile,
	CancelRequest,
	CancelResult,
	ArtifactPage,
	PreflightResult,
	ProblemDetails,
	ProjectHistoryPage,
	ProjectPage,
	ProjectRegistration,
	ProjectSummary,
	ReadyInfo,
	RunAccepted,
	RunPage,
	RunSnapshot,
	RunSpec,
	TaskSubmissionRequest,
	RuntimeCapabilities,
	ShutdownAccepted,
	ModelServiceInput,
	ModelServiceSummary,
	ModelServiceTestResult,
)
from browser_use.webretriever.desktop.task_submission import TaskSubmissionService
from browser_use.webretriever.desktop.run_manager import (
	ActiveProjectRunError,
	ActiveRunExistsError,
	EventsExpiredError,
	IdempotencyKeyReusedError,
	RunManager,
	RunNotFoundError,
	ProjectNotFoundError,
	ProjectDeletionPermissionError,
	ProjectPathUnsafeError,
	SidecarDrainingError,
)
from browser_use.webretriever.desktop.store import ProjectUrlMismatchError
from browser_use.webretriever.desktop.runner_adapter import PreflightError


class _ProblemError(Exception):
	def __init__(self, problem: ProblemDetails) -> None:
		super().__init__(problem.error_code)
		self.problem = problem


def _problem_response(problem: ProblemDetails) -> JSONResponse:
	return JSONResponse(
		status_code=problem.status,
		content=problem.model_dump(mode="json"),
		media_type="application/problem+json",
	)


def create_app(
	*,
	manager: RunManager,
	launch_token: str,
	preflight: Callable[[RunSpec], Awaitable[PreflightResult | dict[str, Any]]] | None = None,
	allowed_origins: tuple[str, ...] = (),
	on_shutdown_requested: Callable[[], Awaitable[None]] | None = None,
	account_profile_store: AccountProfileStore | None = None,
	model_service_store: ModelServiceStore | None = None,
	model_service_tester: Callable[[ModelServiceInput], Awaitable[None]] | None = None,
	sidecar_build: str = "development",
	runner_build: str = "development",
	task_submission_dir: str | os.PathLike[str] | None = None,
	state_dir: str | os.PathLike[str] | None = None,
) -> FastAPI:
	"""Build one sidecar application bound to its launch-scoped bearer token."""

	if not launch_token:
		raise ValueError("launch_token must not be empty")
	if not sidecar_build or not runner_build:
		raise ValueError("sidecar_build and runner_build must not be empty")

	app = FastAPI(
		title="WebRetriever Local Control Plane",
		version="1.0.0",
		docs_url=None,
		redoc_url=None,
		openapi_url=None,
	)
	app.state.run_manager = manager
	app.state.preflight = preflight
	app.state.account_profile_store = account_profile_store or AccountProfileStore()
	app.state.model_service_store = model_service_store or ModelServiceStore()
	app.state.model_service_tester = model_service_tester or test_model_service_connection
	effective_state_dir = state_dir or task_submission_dir or ".webretriever-desktop"
	app.state.state_dir = os.fspath(effective_state_dir)
	app.state.task_submission_service = TaskSubmissionService(effective_state_dir)
	@app.middleware("http")
	async def assign_trace_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
		request.state.trace_id = str(uuid4())
		return await call_next(request)

	app.add_middleware(
		CORSMiddleware,
		allow_origins=list(allowed_origins),
		allow_credentials=False,
		allow_methods=["GET", "POST", "PUT", "DELETE"],
		allow_headers=["Authorization", "Idempotency-Key", "Last-Event-ID", "Content-Type"],
	)

	@app.exception_handler(_ProblemError)
	async def handle_problem(_request: Request, error: _ProblemError) -> JSONResponse:
		return _problem_response(error.problem)

	@app.exception_handler(RequestValidationError)
	async def handle_validation(request: Request, error: RequestValidationError) -> JSONResponse:
		fields: dict[str, list[str]] = {}
		for item in error.errors():
			location = ".".join(str(part) for part in item.get("loc", ()))
			fields.setdefault(location or "request", []).append(str(item.get("msg", "invalid value")))
		return _problem_response(
			ProblemDetails.for_error(
				code="validation_failed",
				status=422,
				instance=request.url.path,
				trace_id=request.state.trace_id,
				errors=fields,
			)
		)

	async def require_launch_bearer(request: Request) -> None:
		authorization = request.headers.get("Authorization", "")
		prefix = "Bearer "
		token = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
		if not token or not hmac.compare_digest(token, launch_token):
			raise _ProblemError(
				ProblemDetails.for_error(
					code="unauthorized",
					status=401,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			)

	@app.get("/api/v1/health/live", status_code=204, dependencies=[Depends(require_launch_bearer)])
	async def get_live_health() -> Response:
		return Response(status_code=204, headers={"Cache-Control": "no-store"})

	@app.get("/api/v1/health/ready", response_model=ReadyInfo, dependencies=[Depends(require_launch_bearer)])
	async def get_ready_health() -> JSONResponse:
		payload = ReadyInfo(sidecar_build=sidecar_build, runner_build=runner_build, pid=os.getpid())
		return JSONResponse(content=payload.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

	@app.get(
		"/api/v1/runtime/capabilities",
		response_model=RuntimeCapabilities,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def get_runtime_capabilities() -> RuntimeCapabilities:
		return RuntimeCapabilities()

	@app.get(
		"/api/v1/account-profile",
		response_model=AccountProfile,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def get_account_profile() -> AccountProfile:
		return app.state.account_profile_store.get()

	@app.put(
		"/api/v1/account-profile",
		response_model=AccountProfile,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def update_account_profile(profile: AccountProfile) -> AccountProfile:
		return app.state.account_profile_store.save(profile)

	@app.get(
		"/api/v1/model-services",
		response_model=list[ModelServiceSummary],
		dependencies=[Depends(require_launch_bearer)],
	)
	async def list_model_services() -> list[ModelServiceSummary]:
		return app.state.model_service_store.list()

	@app.post(
		"/api/v1/model-services",
		status_code=201,
		response_model=ModelServiceSummary,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def add_model_service(service: ModelServiceInput, request: Request) -> ModelServiceSummary:
		try:
			return app.state.model_service_store.add(service)
		except ValueError as exc:
			raise _validation_problem(request, detail="the model service could not be saved") from exc

	@app.post(
		"/api/v1/model-services/test",
		response_model=ModelServiceTestResult,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def test_unsaved_model_service(service: ModelServiceInput) -> ModelServiceTestResult:
		return await _test_model_service_connection(app.state.model_service_tester, service)

	@app.post(
		"/api/v1/model-services/{service_name}/test",
		response_model=ModelServiceTestResult,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def test_saved_model_service(service_name: str, request: Request) -> ModelServiceTestResult:
		try:
			service = app.state.model_service_store.get(service_name)
		except KeyError as exc:
			raise _validation_problem(request, detail="the model service was not found") from exc
		return await _test_model_service_connection(app.state.model_service_tester, service)

	@app.get("/api/v1/projects", response_model=ProjectPage, dependencies=[Depends(require_launch_bearer)])
	async def list_projects() -> ProjectPage:
		return ProjectPage(items=await manager.list_projects())

	@app.put(
		"/api/v1/projects/{project_id}",
		response_model=ProjectSummary,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def register_project(project_id: str, registration: ProjectRegistration, request: Request) -> ProjectSummary:
		_validate_project_id(project_id, request)
		try:
			return await manager.register_project(project_id, registration.website_url)
		except ProjectUrlMismatchError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_url_mismatch",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc

	@app.get(
		"/api/v1/projects/{project_id}/history",
		response_model=ProjectHistoryPage,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def list_project_history(
		project_id: str,
		request: Request,
		cursor: str | None = None,
		limit: int = 20,
	) -> ProjectHistoryPage:
		_validate_project_id(project_id, request)
		try:
			items, next_cursor = await manager.list_project_history(project_id, cursor=cursor, limit=limit)
		except ProjectNotFoundError as exc:
			raise _project_not_found_problem(request, project_id) from exc
		return ProjectHistoryPage(items=items, next_cursor=next_cursor)

	@app.delete(
		"/api/v1/projects/{project_id}",
		status_code=204,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def delete_project(project_id: str, request: Request) -> Response:
		_validate_project_id(project_id, request)
		try:
			await manager.delete_project(project_id, state_dir=app.state.state_dir)
		except ProjectNotFoundError as exc:
			raise _project_not_found_problem(request, project_id) from exc
		except ActiveProjectRunError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_has_active_run",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
					run_id=exc.run_id,
				)
			) from exc
		except ProjectPathUnsafeError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_path_unsafe",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc
		except ProjectDeletionPermissionError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_files_in_use",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
					errors={"diagnostic": [exc.stage]},
				)
			) from exc
		except Exception as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_delete_failed",
					status=500,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc
		return Response(status_code=204)

	@app.post(
		"/api/v1/run-preflights",
		response_model=PreflightResult,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def preflight_run(spec: RunSpec, request: Request) -> PreflightResult:
		if preflight is None:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="sidecar_draining",
					status=503,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			)
		try:
			return PreflightResult.model_validate(await preflight(spec))
		except PreflightError as exc:
			raise _validation_problem(request, detail=str(exc)) from exc

	@app.post(
		"/api/v1/runs",
		status_code=202,
		response_model=RunAccepted,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def create_run(
		spec: RunSpec,
		request: Request,
		idempotency_key: str = Header(alias="Idempotency-Key"),
	) -> RunAccepted:
		try:
			key = str(UUID(idempotency_key))
		except (ValueError, AttributeError) as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="validation_failed",
					status=422,
					instance=request.url.path,
					trace_id=request.state.trace_id,
					errors={"header.Idempotency-Key": ["must be a UUID"]},
				)
			) from exc
		if preflight is None:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="sidecar_draining",
					status=503,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			)
		try:
			await preflight(spec)
		except PreflightError as exc:
			raise _validation_problem(request, detail=str(exc)) from exc
		try:
			return await manager.create_run(spec=spec, idempotency_key=key)
		except ActiveRunExistsError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="active_run_exists",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
					run_id=exc.run_id,
				)
			) from exc
		except IdempotencyKeyReusedError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="idempotency_key_reused",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc
		except ProjectUrlMismatchError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="project_url_mismatch",
					status=409,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc
		except SidecarDrainingError as exc:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="sidecar_draining",
					status=503,
					instance=request.url.path,
					trace_id=request.state.trace_id,
				)
			) from exc

	@app.post(
		"/api/v1/task-submissions",
		status_code=202,
		response_model=RunAccepted,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def submit_task(
		submission: TaskSubmissionRequest,
		request: Request,
		idempotency_key: str = Header(alias="Idempotency-Key"),
	) -> RunAccepted:
		spec = app.state.task_submission_service.create_run_spec(submission)
		return await create_run(spec, request, idempotency_key)

	@app.get("/api/v1/runs", response_model=RunPage, dependencies=[Depends(require_launch_bearer)])
	async def list_runs(cursor: str | None = None, limit: int = 50) -> RunPage:
		items, next_cursor = await manager.list_runs(cursor=cursor, limit=limit)
		return RunPage(items=items, next_cursor=next_cursor)

	@app.get("/api/v1/runs/{run_id}", response_model=RunSnapshot, dependencies=[Depends(require_launch_bearer)])
	async def get_run(run_id: str, request: Request) -> RunSnapshot:
		return await _get_run_or_problem(manager, run_id=run_id, request=request)

	@app.post(
		"/api/v1/runs/{run_id}/cancel",
		response_model=CancelResult,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def cancel_run(run_id: str, request: Request, body: CancelRequest | None = None) -> JSONResponse:
		try:
			snapshot, applied = await manager.request_cancel(run_id, reason=body.reason if body is not None else None)
		except RunNotFoundError as exc:
			raise _not_found_problem(request, run_id) from exc
		result = CancelResult(run_id=run_id, status=snapshot.status, cancel_applied=applied)
		return JSONResponse(status_code=202 if applied else 200, content=result.model_dump(mode="json"))

	@app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(require_launch_bearer)])
	async def stream_run_events(
		run_id: str,
		request: Request,
		after: int = 0,
		last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
	) -> Response:
		cursor = last_event_id if last_event_id is not None else after
		try:
			# Validate the replay boundary before returning streaming headers.  Once
			# the response starts it is too late to communicate a 404/410 problem.
			await manager.events_after(run_id, after=cursor)
		except RunNotFoundError as exc:
			raise _not_found_problem(request, run_id) from exc
		except EventsExpiredError as exc:
			problem = ProblemDetails.for_error(
				code="events_expired",
				status=410,
				instance=request.url.path,
				trace_id=request.state.trace_id,
				run_id=run_id,
			)
			return JSONResponse(
				status_code=410,
				media_type="application/problem+json",
				content={
					**problem.model_dump(mode="json"),
					"minimum_event_id": exc.minimum_event_id,
					"snapshot_url": f"/api/v1/runs/{run_id}",
				},
			)

		async def events() -> Any:
			async for event in manager.stream_events(run_id, after=cursor):
				yield {
					"id": str(event.event_id),
					"event": event.type,
					"retry": 2_000,
					"data": event.model_dump_json(by_alias=True),
				}

		return EventSourceResponse(
			events(),
			ping=15,
			headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
		)

	@app.get(
		"/api/v1/runs/{run_id}/artifacts",
		response_model=ArtifactPage,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def list_artifacts(run_id: str, request: Request, cursor: str | None = None, limit: int = 50) -> ArtifactPage:
		try:
			items, next_cursor = await manager.list_artifacts(run_id, cursor=cursor, limit=limit)
		except RunNotFoundError as exc:
			raise _not_found_problem(request, run_id) from exc
		return ArtifactPage(items=items, next_cursor=next_cursor)

	@app.get("/api/v1/runs/{run_id}/artifacts/{artifact_id}", dependencies=[Depends(require_launch_bearer)])
	async def get_artifact(run_id: str, artifact_id: str, request: Request) -> Response:
		try:
			path = await manager.resolve_artifact(run_id, artifact_id)
		except RunNotFoundError as exc:
			raise _not_found_problem(request, run_id) from exc
		if path is None:
			raise _ProblemError(
				ProblemDetails.for_error(
					code="artifact_not_found",
					status=404,
					instance=request.url.path,
					trace_id=request.state.trace_id,
					run_id=run_id,
				)
			)
		return FileResponse(path)

	@app.post(
		"/api/v1/control/shutdown",
		status_code=202,
		response_model=ShutdownAccepted,
		dependencies=[Depends(require_launch_bearer)],
	)
	async def request_shutdown() -> ShutdownAccepted:
		await manager.request_shutdown()
		if on_shutdown_requested is not None:
			await on_shutdown_requested()
		return ShutdownAccepted()

	return app


async def _get_run_or_problem(manager: RunManager, *, run_id: str, request: Request) -> RunSnapshot:
	try:
		return await manager.get_run(run_id)
	except RunNotFoundError as exc:
		raise _not_found_problem(request, run_id) from exc


def _not_found_problem(request: Request, run_id: str) -> _ProblemError:
	return _ProblemError(
		ProblemDetails.for_error(
			code="run_not_found",
			status=404,
			instance=request.url.path,
			trace_id=request.state.trace_id,
			run_id=run_id,
		)
	)


def _validation_problem(request: Request, *, detail: str) -> _ProblemError:
	return _ProblemError(
		ProblemDetails.for_error(
			code="validation_failed",
			status=422,
			instance=request.url.path,
			trace_id=request.state.trace_id,
			errors={"request": [detail]},
		)
	)


def _project_not_found_problem(request: Request, project_id: str) -> _ProblemError:
	return _ProblemError(
		ProblemDetails.for_error(
			code="project_not_found",
			status=404,
			instance=request.url.path,
			trace_id=request.state.trace_id,
		)
	)


def _validate_project_id(project_id: str, request: Request) -> None:
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id):
		raise _ProblemError(
			ProblemDetails.for_error(
				code="validation_failed",
				status=422,
				instance=request.url.path,
				trace_id=request.state.trace_id,
				errors={"path.project_id": ["must be a safe project identifier"]},
			)
		)


async def _test_model_service_connection(
	tester: Callable[[ModelServiceInput], Awaitable[None]], service: ModelServiceInput
) -> ModelServiceTestResult:
	try:
		await tester(service)
	except ModelServiceConnectionError as exc:
		return ModelServiceTestResult(name=service.name, success=False, error_code=exc.error_code)
	except Exception:
		return ModelServiceTestResult(name=service.name, success=False, error_code="connection_failed")
	return ModelServiceTestResult(name=service.name, success=True)


__all__ = ["create_app"]
