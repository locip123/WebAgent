import type { BackendDescriptor } from "../bridge";
import type { RunSnapshot } from "../runProjection";
import type { RunEvent } from "../runProjection";

export interface RunSpec {
  schema_version: 1;
  input_path: string;
  output_root: string;
  model: { profile_id: string };
  browser: { mode: "local"; headed: boolean };
  limits: {
    max_concurrency: number;
    max_steps: number;
    model_timeout_seconds: number;
    task_timeout_seconds: number;
  };
  selection: {
    task_indices?: number[];
    limit?: number;
  };
}

export interface PreflightResult {
  schema_version: 1;
  task_count: number;
  warnings: string[];
}

export interface RunAccepted {
  schema_version: 1;
  run_id: string;
  status: "STARTING";
  created_at: string;
  snapshot_url: string;
  events_url: string;
}

export interface ControlPlaneApi {
  preflight(spec: RunSpec): Promise<PreflightResult>;
  createRun(spec: RunSpec): Promise<RunAccepted>;
  getRun(runId: string): Promise<RunSnapshot>;
  subscribeToRun(runId: string, after: number, onEvent: (event: RunEvent) => void): Promise<() => void>;
}

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

export class LocalControlPlaneProblem extends Error {
  public constructor(
    public readonly status: number,
    public readonly errorCode: string
  ) {
    super(`Local control plane request failed with HTTP ${status}`);
  }
}

const chineseProblemMessages: Record<string, string> = {
  unauthorized: "本地后端认证已失效，正在重新获取连接。",
  validation_failed: "请求参数无效，请检查填写内容。",
  run_not_found: "找不到该运行记录。",
  artifact_not_found: "找不到该工件。",
  sidecar_draining: "本地后端正在退出，暂时不能开始运行。",
  active_run_exists: "已有运行正在执行，请先等待或取消它。",
  idempotency_key_reused: "本次运行请求已失效，请重新发起。",
  events_expired: "事件记录已过期，正在从最新快照恢复。"
};

export function localizeProblem(problem: LocalControlPlaneProblem): string {
  return chineseProblemMessages[problem.errorCode] ?? "本地后端请求失败，请稍后重试。";
}

export class ControlPlaneClient implements ControlPlaneApi {
  public constructor(
    private readonly descriptor: BackendDescriptor,
    private readonly fetcher: Fetcher = fetch
  ) {}

  public async preflight(spec: RunSpec): Promise<PreflightResult> {
    return this.request<PreflightResult>("/api/v1/run-preflights", {
      method: "POST",
      body: JSON.stringify(spec)
    });
  }

  public async createRun(spec: RunSpec): Promise<RunAccepted> {
    return this.request<RunAccepted>("/api/v1/runs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(spec)
    });
  }

  public async getRun(runId: string): Promise<RunSnapshot> {
    return this.request<RunSnapshot>(`/api/v1/runs/${encodeURIComponent(runId)}`, { method: "GET" });
  }

  public async subscribeToRun(
    runId: string,
    after: number,
    onEvent: (event: RunEvent) => void
  ): Promise<() => void> {
    const controller = new AbortController();
    const response = await this.fetcher(`${this.descriptor.baseUrl}/api/v1/runs/${encodeURIComponent(runId)}/events`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${this.descriptor.bearerToken}`,
        Accept: "text/event-stream",
        "Last-Event-ID": String(after)
      },
      signal: controller.signal
    });
    if (!response.ok || !response.body) {
      throw new Error(`Unable to open the local event stream (HTTP ${response.status})`);
    }
    void consumeSse(response.body, onEvent, controller.signal);
    return () => controller.abort();
  }

  private async request<T>(path: string, init: RequestInit): Promise<T> {
    const response = await this.fetcher(`${this.descriptor.baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.descriptor.bearerToken}`,
        "Content-Type": "application/json",
        ...init.headers
      }
    });
    if (!response.ok) {
      throw await problemFrom(response);
    }
    return response.json() as Promise<T>;
  }
}

async function problemFrom(response: Response): Promise<LocalControlPlaneProblem> {
  try {
    const payload: unknown = await response.json();
    if (typeof payload === "object" && payload !== null && "error_code" in payload && typeof payload.error_code === "string") {
      return new LocalControlPlaneProblem(response.status, payload.error_code);
    }
  } catch {
    // The public API may fail before emitting a Problem Details payload.
  }
  return new LocalControlPlaneProblem(response.status, "unknown");
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffered += decoder.decode(chunk.value, { stream: true });
      const blocks = buffered.split(/\r?\n\r?\n/);
      buffered = blocks.pop() ?? "";
      for (const block of blocks) {
        const data = block
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice("data:".length).trimStart())
          .join("\n");
        if (!data) continue;
        try {
          onEvent(JSON.parse(data) as RunEvent);
        } catch {
          // A malformed server event never becomes UI state; reconnect logic
          // will use the last successfully applied durable cursor.
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
