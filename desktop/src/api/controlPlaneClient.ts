import type { BackendDescriptor } from "../bridge";
import type { RunSnapshot } from "../runProjection";
import type { RunEvent } from "../runProjection";

export interface RunSpec {
  schema_version: 1;
  input_path: string;
  output_root: string;
  project_id?: string;
  project_url?: string;
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

export interface AccountProfile {
  name: string;
  email: string;
  age: number | null;
  work: string;
  organization: string;
}

export interface ModelServiceSummary {
  name: string;
  api_base: string;
  model: string;
  response_mode: "responses" | "chat-completions";
}

export interface ModelServiceInput extends ModelServiceSummary {
  api_key: string;
}

export interface ModelServiceTestResult {
  name: string;
  success: boolean;
  error_code: string | null;
}

export interface RunAccepted {
  schema_version: 1;
  run_id: string;
  status: "STARTING";
  created_at: string;
  snapshot_url: string;
  events_url: string;
}

export interface CancelResult {
  schema_version: 1;
  run_id: string;
  status: "STARTING" | "RUNNING" | "CANCELLING" | "COMPLETED" | "CANCELLED" | "FAILED" | "INTERRUPTED";
  cancel_applied: boolean;
}

export interface TaskSubmission {
  task: string;
  website_url: string;
  project_id?: string;
}

export interface ProjectSummary {
  schema_version: 1;
  project_id: string;
  website_url: string;
  created_at: string;
}

export interface ProjectRegistration {
  website_url: string;
}

export interface ProjectHistoryItem {
  schema_version: 1;
  run_id: string;
  project_id: string | null;
  project_url: string | null;
  status: RunSnapshot["status"];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  output_dir: string;
  last_event_id: number;
  summary: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  instruction: string | null;
  events: RunEvent[];
}

export interface ProjectHistoryPage {
  schema_version: 1;
  items: ProjectHistoryItem[];
  next_cursor: string | null;
}

export interface ProjectHistoryQuery {
  cursor?: string;
  limit?: number;
}

export interface ControlPlaneApi {
	getAccountProfile?(): Promise<AccountProfile>;
	updateAccountProfile?(profile: AccountProfile): Promise<AccountProfile>;
  listModelServices?(): Promise<ModelServiceSummary[]>;
  addModelService?(service: ModelServiceInput): Promise<ModelServiceSummary>;
  testModelService?(service: ModelServiceInput): Promise<ModelServiceTestResult>;
  testSavedModelService?(name: string): Promise<ModelServiceTestResult>;
  preflight(spec: RunSpec): Promise<PreflightResult>;
  createRun(spec: RunSpec): Promise<RunAccepted>;
	 submitTask?(submission: TaskSubmission): Promise<RunAccepted>;
  getRun(runId: string): Promise<RunSnapshot>;
	 cancelRun?(runId: string): Promise<CancelResult>;
  registerProject?(projectId: string, registration: ProjectRegistration): Promise<ProjectSummary>;
  deleteProject?(projectId: string): Promise<void>;
  listProjectHistory?(projectId: string, query?: ProjectHistoryQuery): Promise<ProjectHistoryPage>;
  getProjectHistory?(projectId: string, query?: ProjectHistoryQuery): Promise<ProjectHistoryPage>;
  subscribeToRun(runId: string, after: number, onEvent: (event: RunEvent) => void): Promise<() => void>;
}

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

export class LocalControlPlaneProblem extends Error {
  public constructor(
    public readonly status: number,
    public readonly errorCode: string,
    public readonly diagnostic?: string
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
  events_expired: "事件记录已过期，正在从最新快照恢复。",
  project_not_found: "项目不存在，可能已在其他窗口中删除。",
  project_has_active_run: "该项目仍有任务正在运行，请先停止任务后再删除。",
  project_url_mismatch: "项目网址与后端记录不一致，无法继续操作。",
	project_path_unsafe: "项目文件路径不在本地工作区内，已拒绝删除。",
	project_delete_failed: "删除项目失败，请稍后重试。"
};

export function localizeProblem(problem: LocalControlPlaneProblem): string {
	if (problem.errorCode === "project_files_in_use") {
		const stageMessages: Record<string, string> = {
			prepare_storage: "无法访问项目存储位置",
			stage_files: "项目文件正被占用或存储位置不可写",
			delete_records: "本地项目记录不可写",
			rollback: "恢复项目文件时权限不足"
		};
		const stage = problem.diagnostic ? stageMessages[problem.diagnostic] : undefined;
		return `删除项目失败：${stage ?? "本地项目资源不可用"}。请关闭资源管理器预览、浏览器和其他可能打开项目文件的程序后重试。`;
	}
  return chineseProblemMessages[problem.errorCode] ?? `本地后端请求失败（HTTP ${problem.status}），请稍后重试。`;
}

export class ControlPlaneClient implements ControlPlaneApi {
  public constructor(
    private readonly descriptor: BackendDescriptor,
    private readonly fetcher: Fetcher = (input, init) => globalThis.fetch(input, init)
  ) {}

  public async preflight(spec: RunSpec): Promise<PreflightResult> {
    return this.request<PreflightResult>("/api/v1/run-preflights", {
      method: "POST",
      body: JSON.stringify(spec)
    });
  }

	public async getAccountProfile(): Promise<AccountProfile> {
		return this.request<AccountProfile>("/api/v1/account-profile", { method: "GET" });
	}

	public async updateAccountProfile(profile: AccountProfile): Promise<AccountProfile> {
		return this.request<AccountProfile>("/api/v1/account-profile", {
			method: "PUT",
			body: JSON.stringify(profile)
		});
	}

  public async listModelServices(): Promise<ModelServiceSummary[]> {
    return this.request<ModelServiceSummary[]>("/api/v1/model-services", { method: "GET" });
  }

  public async addModelService(service: ModelServiceInput): Promise<ModelServiceSummary> {
    return this.request<ModelServiceSummary>("/api/v1/model-services", {
      method: "POST",
      body: JSON.stringify(service)
    });
  }

  public async testModelService(service: ModelServiceInput): Promise<ModelServiceTestResult> {
    return this.request<ModelServiceTestResult>("/api/v1/model-services/test", {
      method: "POST",
      body: JSON.stringify(service)
    });
  }

  public async testSavedModelService(name: string): Promise<ModelServiceTestResult> {
    return this.request<ModelServiceTestResult>(`/api/v1/model-services/${encodeURIComponent(name)}/test`, {
      method: "POST"
    });
  }

  public async createRun(spec: RunSpec): Promise<RunAccepted> {
    return this.request<RunAccepted>("/api/v1/runs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(spec)
    });
  }

	public async submitTask(submission: TaskSubmission): Promise<RunAccepted> {
		return this.request<RunAccepted>("/api/v1/task-submissions", {
			method: "POST",
			headers: { "Idempotency-Key": crypto.randomUUID() },
			body: JSON.stringify(submission)
		});
	}

  public async getRun(runId: string): Promise<RunSnapshot> {
    return this.request<RunSnapshot>(`/api/v1/runs/${encodeURIComponent(runId)}`, { method: "GET" });
  }

  public async cancelRun(runId: string): Promise<CancelResult> {
    return this.request<CancelResult>(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  }

  public async registerProject(projectId: string, registration: ProjectRegistration): Promise<ProjectSummary> {
    return this.request<ProjectSummary>(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
      method: "PUT",
      body: JSON.stringify(registration)
    });
  }

  public async deleteProject(projectId: string): Promise<void> {
    await this.request<void>(`/api/v1/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
  }

  public async listProjectHistory(projectId: string, query: ProjectHistoryQuery = {}): Promise<ProjectHistoryPage> {
    const params = new URLSearchParams();
    if (query.cursor) params.set("cursor", query.cursor);
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return this.request<ProjectHistoryPage>(`/api/v1/projects/${encodeURIComponent(projectId)}/history${suffix}`, {
      method: "GET"
    });
  }

  public async getProjectHistory(projectId: string, query: ProjectHistoryQuery = {}): Promise<ProjectHistoryPage> {
    return this.listProjectHistory(projectId, query);
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
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }
}

async function problemFrom(response: Response): Promise<LocalControlPlaneProblem> {
  try {
    const payload: unknown = await response.json();
    if (typeof payload === "object" && payload !== null && "error_code" in payload && typeof payload.error_code === "string") {
			const errors = "errors" in payload && typeof payload.errors === "object" && payload.errors !== null
				? payload.errors as Record<string, unknown>
				: null;
			const diagnostic = errors && Array.isArray(errors.diagnostic) && typeof errors.diagnostic[0] === "string"
				? errors.diagnostic[0]
				: undefined;
      return new LocalControlPlaneProblem(response.status, payload.error_code, diagnostic);
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
