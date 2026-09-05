import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";

describe("desktop run workspace", () => {
	const readyBridge = () => ({
		getDescriptor: vi.fn().mockResolvedValue({
			baseUrl: "http://127.0.0.1:43127",
			bearerToken: "ephemeral-token",
			protocolVersion: 1
		}),
		retrySidecar: vi.fn(),
		onBackendStateChanged: vi.fn().mockResolvedValue(() => {}),
		pickTaskFile: vi.fn().mockResolvedValue(null),
		pickOutputDirectory: vi.fn().mockResolvedValue(null)
	});

	it("enables preflight only after the native shell publishes a ready descriptor", async () => {
	const bridge = readyBridge();

    render(<App bridge={bridge} />);

    expect(await screen.findByText("后端已就绪")).toBeVisible();
    expect(screen.getByRole("button", { name: "预检" })).toBeEnabled();
		expect(screen.getByRole("button", { name: "开始运行" })).toBeDisabled();
	});

	it("shows the validated task count after a user preflights a selected run", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn().mockResolvedValue({ schema_version: 1, task_count: 2, warnings: [] }),
			createRun: vi.fn(),
			getRun: vi.fn(),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};

		render(<App bridge={readyBridge()} createClient={() => client} />);
		await screen.findByText("后端已就绪");
		await user.type(screen.getByLabelText("任务文件"), "/work/tasks.json");
		await user.type(screen.getByLabelText("输出目录"), "/work/outputs");
		await user.type(screen.getByLabelText("模型配置"), "local-default");
		await user.click(screen.getByRole("button", { name: "预检" }));

		expect(await screen.findByText("预检通过：2 个任务")).toBeVisible();
	});

	it("fills the task input from the native file dialog result", async () => {
		const user = userEvent.setup();
		const bridge = readyBridge();
		bridge.pickTaskFile.mockResolvedValue("/work/selected-tasks.json");

		render(<App bridge={bridge} />);
		await screen.findByText("后端已就绪");
		await user.click(screen.getByRole("button", { name: "选择任务文件" }));

		expect(screen.getByLabelText("任务文件")).toHaveValue("/work/selected-tasks.json");
	});

	it("starts only a preflighted run and identifies the active run from the server response", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn().mockResolvedValue({ schema_version: 1, task_count: 1, warnings: [] }),
			createRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-7",
				status: "STARTING",
				created_at: "2026-09-04T12:00:00Z",
				snapshot_url: "/api/v1/runs/run-7",
				events_url: "/api/v1/runs/run-7/events"
			}),
			getRun: vi.fn().mockResolvedValue({
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
			}),
			subscribeToRun: vi.fn().mockImplementation(async (_runId, _after, onEvent) => {
				onEvent({
					schema: "webretriever.run-event/v1",
					run_id: "run-7",
					event_id: 2,
					type: "task.started",
					occurred_at: "2026-09-04T12:00:02Z",
					level: "info",
					task: { task_id: "task-4", task_idx: 4 },
					payload: { website_display: "example.com" }
				});
				onEvent({
					schema: "webretriever.run-event/v1",
					run_id: "run-7",
					event_id: 3,
					type: "run.failed",
					occurred_at: "2026-09-04T12:00:03Z",
					level: "error",
					task: null,
					payload: { error: { code: "runner_failed", message: "raw sidecar diagnostic" } }
				});
				return () => {};
			})
		};

		render(<App bridge={readyBridge()} createClient={() => client} />);
		await screen.findByText("后端已就绪");
		await user.type(screen.getByLabelText("任务文件"), "/work/tasks.json");
		await user.type(screen.getByLabelText("输出目录"), "/work/outputs");
		await user.type(screen.getByLabelText("模型配置"), "local-default");
		expect(screen.getByRole("button", { name: "开始运行" })).toBeDisabled();
		await user.click(screen.getByRole("button", { name: "预检" }));
		await screen.findByText("预检通过：1 个任务");
		await user.click(screen.getByRole("button", { name: "开始运行" }));

		expect(await screen.findByText("运行 run-7 已开始")).toBeVisible();
		expect(await screen.findByText("运行状态：FAILED")).toBeVisible();
		expect(await screen.findByText("任务 4：task-4（example.com）")).toBeVisible();
		expect(await screen.findByText("runner_failed")).toBeVisible();
	});
});
