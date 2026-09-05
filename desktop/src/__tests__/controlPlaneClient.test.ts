import { describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { ControlPlaneClient, LocalControlPlaneProblem, localizeProblem, type RunSpec } from "../api/controlPlaneClient";

const descriptor = {
  baseUrl: "http://127.0.0.1:43127",
  bearerToken: "ephemeral-token",
  protocolVersion: 1
};

const runSpec: RunSpec = {
  schema_version: 1,
  input_path: "/work/tasks.json",
  output_root: "/work/outputs",
  model: { profile_id: "local-default" },
  browser: { mode: "local", headed: false },
  limits: {
    max_concurrency: 1,
    max_steps: 100,
    model_timeout_seconds: 180,
    task_timeout_seconds: 60
  },
  selection: {}
};

describe("local control-plane client", () => {
  it("preflights a run using the launch-scoped bearer token in a request header", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ schema_version: 1, task_count: 2, warnings: [] }), { status: 200 })
    );
    const client = new ControlPlaneClient(descriptor, fetcher);

    await expect(client.preflight(runSpec)).resolves.toEqual({ schema_version: 1, task_count: 2, warnings: [] });

    const [url, options] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:43127/api/v1/run-preflights");
    expect(new Headers(options.headers).get("Authorization")).toBe("Bearer ephemeral-token");
    expect(JSON.parse(String(options.body))).toEqual(runSpec);
  });

  it("replays SSE events from the supplied cursor with the bearer token kept in headers", async () => {
    const encoder = new TextEncoder();
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode("id: 3\nevent: task.phase_changed\ndata: {\"schema\":\"webretriever.run-event/v1\",\"run_id\":\"run-7\",\"event_id\":3,\"type\":\"task.phase_changed\",\"occurred_at\":\"2026-09-04T12:00:03Z\",\"level\":\"info\",\"task\":null,\"payload\":{}}\n\n"));
            controller.close();
          }
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } }
      )
    );
    const client = new ControlPlaneClient(descriptor, fetcher);
    const received: string[] = [];

    await client.subscribeToRun("run-7", 2, (event) => received.push(event.type));

    await waitFor(() => expect(received).toEqual(["task.phase_changed"]));
    const [url, options] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:43127/api/v1/runs/run-7/events");
    expect(new Headers(options.headers).get("Last-Event-ID")).toBe("2");
    expect(new Headers(options.headers).get("Authorization")).toBe("Bearer ephemeral-token");
  });

  it("turns a stable problem code into a Chinese UI message without using server detail text", async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(
        JSON.stringify({ error_code: "active_run_exists", detail: "raw sidecar diagnostic should not reach the UI" }),
        { status: 409, headers: { "Content-Type": "application/problem+json" } }
      )
    ));
    const client = new ControlPlaneClient(descriptor, fetcher);

    await expect(client.preflight(runSpec)).rejects.toBeInstanceOf(LocalControlPlaneProblem);
    await expect(client.preflight(runSpec)).rejects.toMatchObject({ errorCode: "active_run_exists" });
    expect(localizeProblem(new LocalControlPlaneProblem(409, "active_run_exists"))).toBe("已有运行正在执行，请先等待或取消它。");
  });
});
