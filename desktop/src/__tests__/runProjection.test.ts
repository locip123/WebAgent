import { describe, expect, it } from "vitest";
import { applyRunEvent, createRunProjection, type RunEvent, type RunSnapshot } from "../runProjection";

const snapshot: RunSnapshot = {
  schema_version: 1,
  run_id: "run-7",
  status: "RUNNING",
  created_at: "2026-09-04T12:00:00Z",
  started_at: "2026-09-04T12:00:01Z",
  finished_at: null,
  output_dir: "/work/outputs/run-7",
  last_event_id: 1,
  summary: null,
  error: null
};

const taskStarted: RunEvent = {
  schema: "webretriever.run-event/v1",
  run_id: "run-7",
  event_id: 2,
  type: "task.started",
  occurred_at: "2026-09-04T12:00:02Z",
  level: "info",
  task: { task_id: "task-4", task_idx: 4 },
  payload: { website_display: "example.com" }
};

describe("run event projection", () => {
  it("shows a newly started task once even if the SSE stream replays that event", () => {
    const afterFirstDelivery = applyRunEvent(createRunProjection(snapshot), taskStarted);
    const afterReplay = applyRunEvent(afterFirstDelivery, taskStarted);

    expect(afterReplay).toEqual({
      snapshot: { ...snapshot, last_event_id: 2 },
      tasks: {
        "task-4": {
          taskId: "task-4",
          taskIdx: 4,
          website: "example.com",
          phase: null,
          status: "RUNNING",
          completedSteps: 0,
          maxSteps: null,
          answer: null
        }
      },
      steps: [],
      artifacts: [],
      errors: []
    });
  });

  it("projects task progress and sidecar-issued artifacts into the workspace", () => {
    const started = applyRunEvent(createRunProjection(snapshot), taskStarted);
    const phaseChanged = applyRunEvent(started, {
      ...taskStarted,
      event_id: 3,
      type: "task.phase_changed",
      payload: { phase: "browser_action" }
    });
    const withArtifact = applyRunEvent(phaseChanged, {
      ...taskStarted,
      event_id: 4,
      type: "artifact.available",
      payload: {
        artifact_id: "art-9",
        kind: "result",
        mime_type: "application/json",
        size: 128
      }
    });

    expect(withArtifact.tasks["task-4"].phase).toBe("browser_action");
    expect(withArtifact.artifacts).toEqual([
      {
        artifactId: "art-9",
        kind: "result",
        mimeType: "application/json",
        size: 128,
        taskId: "task-4"
      }
    ]);
  });

  it("keeps each completed step as a model progress update", () => {
    const started = applyRunEvent(createRunProjection(snapshot), taskStarted);
    const progressed = applyRunEvent(started, {
      ...taskStarted,
      event_id: 3,
      type: "task.step.completed",
      payload: { step: 2, max_steps: 5, action: "find_text", outcome: "ok" }
    });

    expect(progressed.tasks["task-4"]).toMatchObject({
      completedSteps: 2,
      maxSteps: 5
    });
    expect(progressed.steps).toEqual([
      {
        eventId: 3,
        taskId: "task-4",
        step: 2,
        maxSteps: 5,
        action: "find_text",
        outcome: "ok"
      }
    ]);
  });

  it("keeps durable task and run failures visible without exposing a raw sidecar diagnostic", () => {
    const started = applyRunEvent(createRunProjection(snapshot), taskStarted);
    const taskFailed = applyRunEvent(started, {
      ...taskStarted,
      event_id: 3,
      type: "task.failed",
      level: "error",
      payload: { error: { code: "target_error", message: "private diagnostic" } }
    });
    const runFailed = applyRunEvent(taskFailed, {
      ...taskStarted,
      event_id: 4,
      type: "run.failed",
      task: null,
      level: "error",
      payload: { error: { code: "runner_failed", message: "private diagnostic" } }
    });

    expect(runFailed.snapshot.status).toBe("FAILED");
    expect(runFailed.tasks["task-4"].status).toBe("FAILED");
    expect(runFailed.errors).toEqual([
      { eventId: 3, code: "target_error", taskId: "task-4" },
      { eventId: 4, code: "runner_failed", taskId: null }
    ]);
  });
});
