export type RunStatus = "STARTING" | "RUNNING" | "CANCELLING" | "COMPLETED" | "CANCELLED" | "FAILED" | "INTERRUPTED";

export interface RunSnapshot {
  schema_version: 1;
  run_id: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  output_dir: string;
  last_event_id: number;
  summary: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
}

export interface RunEvent {
  schema: "webretriever.run-event/v1";
  run_id: string;
  event_id: number;
  type: string;
  occurred_at: string;
  level: "debug" | "info" | "warning" | "error";
  task: { task_id: string; task_idx: number } | null;
  payload: Record<string, unknown>;
}

export interface TaskProjection {
  taskId: string;
  taskIdx: number;
  website: string;
  phase: string | null;
  thinkingStartedAt?: string;
  status: "RUNNING" | "FINISHED" | "FAILED";
  completedSteps: number;
  maxSteps: number | null;
  answer: string | null;
}

export interface TaskStepProjection {
  eventId: number;
  taskId: string;
  step: number;
  maxSteps: number;
  startedAt: string;
  completedAt: string | null;
  action: string;
  outcome: string | null;
  thought: string;
  status: "PENDING" | "COMPLETED";
}

export interface ArtifactProjection {
  artifactId: string;
  kind: string;
  mimeType: string;
  size: number;
  taskId: string | null;
}

export interface RunProjection {
  snapshot: RunSnapshot;
  tasks: Record<string, TaskProjection>;
  steps: TaskStepProjection[];
  artifacts: ArtifactProjection[];
  errors: Array<{ eventId: number; code: string; message?: string; taskId: string | null }>;
}

export function createRunProjection(snapshot: RunSnapshot): RunProjection {
  return { snapshot, tasks: {}, steps: [], artifacts: [], errors: [] };
}

export function applyRunEvent(projection: RunProjection, event: RunEvent): RunProjection {
  if (event.run_id !== projection.snapshot.run_id || event.event_id <= projection.snapshot.last_event_id) {
    return projection;
  }

  const next: RunProjection = {
    ...projection,
    snapshot: { ...projection.snapshot, last_event_id: event.event_id },
    tasks: { ...projection.tasks },
    steps: [...projection.steps],
    errors: [...projection.errors]
  };
  if (event.type === "task.started" && event.task) {
    next.tasks[event.task.task_id] = {
      taskId: event.task.task_id,
      taskIdx: event.task.task_idx,
      website: typeof event.payload.website_display === "string" ? event.payload.website_display : "未知站点",
      phase: null,
      status: "RUNNING",
      completedSteps: 0,
      maxSteps: null,
      answer: null
    };
  }
  if (event.type === "task.phase_changed" && event.task) {
    const task = next.tasks[event.task.task_id];
    if (task) {
      next.tasks[event.task.task_id] = {
        ...task,
        phase: typeof event.payload.phase === "string" ? event.payload.phase : task.phase,
        ...(event.payload.phase === "model_wait" ? { thinkingStartedAt: event.occurred_at } : {})
      };
    }
  }
  if (event.type === "task.step.completed" && event.task) {
    const task = next.tasks[event.task.task_id];
    const step = event.payload.step;
    const maxSteps = event.payload.max_steps;
    const action = event.payload.action;
    const outcome = event.payload.outcome;
    const thought = typeof event.payload.thought === "string" ? event.payload.thought : "";
    if (
      task &&
      typeof step === "number" && Number.isInteger(step) && step > 0 &&
      typeof maxSteps === "number" && Number.isInteger(maxSteps) && maxSteps > 0 &&
      typeof action === "string" &&
      typeof outcome === "string"
    ) {
      next.tasks[event.task.task_id] = {
        ...task,
        completedSteps: step,
        maxSteps
      };
      const existingStepIndex = next.steps.findIndex(
        (candidate) => candidate.taskId === event.task?.task_id && candidate.step === step
      );
      const existingStep = existingStepIndex >= 0 ? next.steps[existingStepIndex] : undefined;
      const completedStep: TaskStepProjection = {
        eventId: existingStep?.eventId ?? event.event_id,
        taskId: event.task.task_id,
        step,
        maxSteps,
        startedAt: existingStep?.startedAt ?? task.thinkingStartedAt ?? event.occurred_at,
        completedAt: existingStep?.completedAt ?? event.occurred_at,
        action,
        outcome,
        thought: thought || existingStep?.thought || "",
        status: "COMPLETED"
      };
      if (existingStepIndex >= 0) {
        next.steps[existingStepIndex] = completedStep;
      } else {
        next.steps.push(completedStep);
      }
    }
  }
  if (event.type === "task.step.decided" && event.task) {
    const task = next.tasks[event.task.task_id];
    const step = event.payload.step;
    const maxSteps = event.payload.max_steps;
    const action = event.payload.action;
    const thought = event.payload.thought;
    if (
      task &&
      typeof step === "number" && Number.isInteger(step) && step > 0 &&
      typeof maxSteps === "number" && Number.isInteger(maxSteps) && maxSteps > 0 &&
      typeof action === "string" &&
      typeof thought === "string"
    ) {
      next.tasks[event.task.task_id] = { ...task, maxSteps };
      next.steps.push({
        eventId: event.event_id,
        taskId: event.task.task_id,
        step,
        maxSteps,
        startedAt: task.thinkingStartedAt ?? event.occurred_at,
        completedAt: event.occurred_at,
        action,
        outcome: null,
        thought,
        status: "PENDING"
      });
    }
  }
  if ((event.type === "task.finished" || event.type === "task.failed") && event.task) {
    const task = next.tasks[event.task.task_id];
    if (task) {
      next.tasks[event.task.task_id] = {
        ...task,
        status: event.type === "task.failed" ? "FAILED" : "FINISHED",
        answer: event.type === "task.finished" && typeof event.payload.answer === "string"
          ? event.payload.answer
          : task.answer
      };
    }
  }
  if (event.type === "artifact.available") {
    const artifactId = event.payload.artifact_id;
    const kind = event.payload.kind;
    const mimeType = event.payload.mime_type;
    const size = event.payload.size;
    if (
      typeof artifactId === "string" &&
      typeof kind === "string" &&
      typeof mimeType === "string" &&
      typeof size === "number" &&
      !projection.artifacts.some((artifact) => artifact.artifactId === artifactId)
    ) {
      next.artifacts = [
        ...projection.artifacts,
        {
          artifactId,
          kind,
          mimeType,
          size,
          taskId: event.task?.task_id ?? null
        }
      ];
    }
  }
  const terminalStatus = terminalRunStatus(event.type);
  const eventError = errorFromEvent(event);
  if (terminalStatus) {
    next.snapshot = {
      ...next.snapshot,
      status: terminalStatus,
      finished_at: event.occurred_at,
      error: event.type === "run.completed" && eventError
        ? eventError
        : event.type === "run.failed" ? { code: problemCode(event) } : next.snapshot.error
    };
  }
  const completedRunError = terminalStatus === "COMPLETED" && eventError;
  const completedTaskError = event.type === "task.finished" && eventError;
  if (event.level === "error" || event.type === "task.failed" || completedRunError || completedTaskError) {
    const projectedError: { eventId: number; code: string; message?: string; taskId: string | null } = {
      eventId: event.event_id,
      code: problemCode(event),
      taskId: event.task?.task_id ?? null
    };
    if ((completedRunError || completedTaskError) && typeof eventError.message === "string") {
      projectedError.message = eventError.message;
    }
    next.errors.push(projectedError);
  }
  return next;
}

function terminalRunStatus(type: string): RunStatus | null {
  if (type === "run.completed") return "COMPLETED";
  if (type === "run.cancelled") return "CANCELLED";
  if (type === "run.failed") return "FAILED";
  if (type === "run.interrupted") return "INTERRUPTED";
  return null;
}

function problemCode(event: RunEvent): string {
  const error = event.payload.error;
  if (typeof error === "object" && error !== null && "code" in error && typeof error.code === "string") {
    return error.code;
  }
  return event.type;
}

function isErrorRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorFromEvent(event: RunEvent): Record<string, unknown> | null {
  if (isErrorRecord(event.payload.error)) return event.payload.error;
  if (typeof event.payload.error === "string" && event.payload.error.trim()) {
    return { code: problemCode(event), message: event.payload.error };
  }
  return null;
}
