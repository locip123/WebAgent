import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { RunEvent } from "../runProjection";

describe("desktop run workspace", () => {
	beforeEach(() => {
		localStorage.clear();
	});

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

	async function openBatchOperations(user: UserEvent) {
		await user.click(screen.getByRole("button", { name: "设置" }));
		await user.click(await screen.findByRole("button", { name: "批量操作" }));
		await screen.findByRole("heading", { name: "批量操作" });
	}

	it("opens on the workspace home with a task prompt and settings navigation", async () => {
		render(<App bridge={readyBridge()} />);

		expect(await screen.findByRole("heading", { name: "你好，林晓宇" })).toBeVisible();
		expect(screen.getByPlaceholderText("描述你的任务，例如：帮我整理一份竞品分析")).toBeVisible();
		expect(screen.getByRole("button", { name: "设置" })).toBeVisible();

		await userEvent.setup().click(screen.getByRole("button", { name: "设置" }));

		expect(await screen.findByRole("heading", { name: "设置" })).toBeVisible();
		expect(screen.getByRole("button", { name: "批量操作" })).toBeVisible();
	});

	it("returns to the workspace without leaving settings selected", async () => {
		const user = userEvent.setup();
		render(<App bridge={readyBridge()} />);

		const settings = screen.getByRole("button", { name: "设置" });
		await user.click(settings);
		expect(settings).toHaveAttribute("aria-current", "page");

		await user.click(screen.getByRole("button", { name: "我的工作区" }));

		expect(await screen.findByRole("heading", { name: "你好，林晓宇" })).toBeVisible();
		expect(settings).not.toHaveAttribute("aria-current");
	});

	it("opens the create-project dialog from the workspace sidebar", async () => {
		const user = userEvent.setup();
		render(<App bridge={readyBridge()} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));

		expect(await screen.findByRole("dialog", { name: "创建项目" })).toBeVisible();
		expect(screen.getByLabelText("网站名称")).toHaveAttribute("placeholder", "例如：我的产品官网");
		expect(screen.getByLabelText("网站 URL")).toHaveAttribute("placeholder", "https://example.com");
		expect(screen.getByRole("button", { name: "取消" })).toBeVisible();
		expect(screen.getByRole("button", { name: "确定" })).toBeVisible();
	});

	it("submits a workspace task as a headed run for the current project", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn().mockResolvedValue({
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
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "我的产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "https://example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));

		expect(screen.queryByRole("dialog", { name: "创建项目" })).not.toBeInTheDocument();
		expect(screen.getByRole("button", { name: "项目 我的产品官网" })).toBeVisible();
		expect(screen.getAllByText("https://example.com").length).toBe(2);

		await user.type(screen.getByLabelText("任务描述"), "整理首页信息");
		await user.click(screen.getByRole("button", { name: "提交任务" }));

		expect(client.submitTask).toHaveBeenCalledWith({
			task: "整理首页信息",
			website_url: "https://example.com"
		});
		expect(await screen.findByText("任务已开始，正在打开浏览器…")).toBeVisible();
	});

	it("clears the startup notice when the submitted task enters a terminal state", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-completed",
				status: "STARTING",
				created_at: "2026-09-04T12:00:00Z",
				snapshot_url: "/api/v1/runs/run-completed",
				events_url: "/api/v1/runs/run-completed/events"
			}),
			getRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-completed",
				status: "RUNNING",
				created_at: "2026-09-04T12:00:00Z",
				started_at: "2026-09-04T12:00:01Z",
				finished_at: null,
				output_dir: "/work/outputs/run-completed",
				last_event_id: 1,
				summary: null,
				error: null
			}),
			subscribeToRun: vi.fn().mockImplementation(async (_runId, _after, onEvent) => {
				onEvent({
					schema: "webretriever.run-event/v1",
					run_id: "run-completed",
					event_id: 2,
					type: "run.completed",
					occurred_at: "2026-09-04T12:00:02Z",
					level: "info",
					task: null,
					payload: {}
				});
				return () => {};
			})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "https://example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));
		await user.type(screen.getByLabelText("任务描述"), "整理首页信息");
		await user.click(screen.getByRole("button", { name: "提交任务" }));

		expect(await screen.findByText("运行状态：COMPLETED")).toBeVisible();
		expect(screen.queryByText("任务已开始，正在打开浏览器…")).not.toBeInTheDocument();
	});

	it("shows an active task as a conversation and lets the user stop it", async () => {
		const user = userEvent.setup();
		let publishEvent: ((event: RunEvent) => void) | null = null;
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-9",
				status: "STARTING",
				created_at: "2026-09-04T12:00:00Z",
				snapshot_url: "/api/v1/runs/run-9",
				events_url: "/api/v1/runs/run-9/events"
			}),
			getRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-9",
				status: "RUNNING",
				created_at: "2026-09-04T12:00:00Z",
				started_at: "2026-09-04T12:00:01Z",
				finished_at: null,
				output_dir: "/work/outputs/run-9",
				last_event_id: 1,
				summary: null,
				error: null
			}),
			cancelRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-9",
				status: "CANCELLING",
				cancel_applied: true
			}),
			subscribeToRun: vi.fn().mockImplementation(async (_runId, _after, onEvent) => {
				publishEvent = onEvent;
				onEvent({
					schema: "webretriever.run-event/v1",
					run_id: "run-9",
					event_id: 2,
					type: "task.started",
					occurred_at: "2026-09-04T12:00:02Z",
					level: "info",
					task: { task_id: "task-9", task_idx: 1 },
					payload: { website_display: "example.com" }
				});
					onEvent({
					schema: "webretriever.run-event/v1",
					run_id: "run-9",
					event_id: 3,
					type: "task.step.decided",
					occurred_at: "2026-09-04T12:00:03Z",
					level: "info",
					task: { task_id: "task-9", task_idx: 1 },
					payload: {
						step: 2,
						max_steps: 5,
						action: "find_text",
						thought: "Inspect the page for the requested text."
					}
				});
				return () => {};
			})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "https://example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));
		await user.type(screen.getByLabelText("任务描述"), "整理首页信息");
		await user.click(screen.getByRole("button", { name: "提交任务" }));

		expect(await screen.findByText("用户指令")).toBeVisible();
		expect(screen.getByText("整理首页信息")).toBeVisible();
		expect(screen.getByText("模型思考过程")).toBeVisible();
		expect(screen.getByText("Inspect the page for the requested text.")).toBeVisible();
		expect(screen.getByText("第 2 / 5 步")).toBeVisible();
		expect(screen.getByText("正在执行 find_text…")).toBeVisible();

		act(() => {
			if (publishEvent === null) {
				throw new Error("SSE callback was not registered");
			}
			publishEvent({
				schema: "webretriever.run-event/v1",
				run_id: "run-9",
				event_id: 4,
				type: "task.step.completed",
				occurred_at: "2026-09-04T12:00:04Z",
				level: "info",
				task: { task_id: "task-9", task_idx: 1 },
				payload: {
					step: 2,
					max_steps: 5,
					action: "find_text",
					outcome: "ok",
					thought: "Inspect the page for the requested text."
				}
			});
			publishEvent({
				schema: "webretriever.run-event/v1",
				run_id: "run-9",
				event_id: 5,
				type: "task.finished",
				occurred_at: "2026-09-04T12:00:05Z",
				level: "info",
				task: { task_id: "task-9", task_idx: 1 },
				payload: { domain_status: "SUCCESS", answer: "首页信息已经整理完成。" }
			});
		});

		expect(await screen.findByText("已执行 find_text，结果：ok")).toBeVisible();
		expect(screen.getByText("模型回答")).toBeVisible();
		expect(screen.getByText("首页信息已经整理完成。")).toBeVisible();

		await user.click(screen.getByRole("button", { name: "停止任务" }));

		expect(client.cancelRun).toHaveBeenCalledWith("run-9");
		expect(await screen.findByText("正在停止任务…")).toBeVisible();
	});

	it("keeps the dialog open and explains invalid project fields", async () => {
		const user = userEvent.setup();
		render(<App bridge={readyBridge()} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.click(screen.getByRole("button", { name: "确定" }));

		expect(screen.getByText("请输入网站名称")).toBeVisible();
		expect(screen.getByText("请输入网站 URL")).toBeVisible();

		await user.type(screen.getByLabelText("网站名称"), "我的产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));

		expect(screen.getByRole("dialog", { name: "创建项目" })).toBeVisible();
		expect(screen.getByText("请输入有效的网站 URL（以 http:// 或 https:// 开头）")).toBeVisible();
		expect(screen.queryByRole("button", { name: /我的产品官网/ })).not.toBeInTheDocument();
	});

	it("persists a created project as the current project after reopening the workspace", async () => {
		const user = userEvent.setup();
		const firstRender = render(<App bridge={readyBridge()} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "产品后台");
		await user.type(screen.getByLabelText("网站 URL"), "https://admin.example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));
		firstRender.unmount();

		render(<App bridge={readyBridge()} />);

		expect(await screen.findByRole("button", { name: "项目 产品后台" })).toBeVisible();
		expect(screen.getAllByText("https://admin.example.com").length).toBe(2);
	});

	it("restores the latest project interaction and clears it when switching to an empty project", async () => {
		const user = userEvent.setup();
		localStorage.setItem("webAgent.projects", JSON.stringify([
			{ id: "project-a", name: "项目 A", websiteUrl: "https://a.example.com", createdAt: "2026-09-01T00:00:00Z" },
			{ id: "project-b", name: "项目 B", websiteUrl: "https://b.example.com", createdAt: "2026-09-02T00:00:00Z" }
		]));
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn(),
			getRun: vi.fn(),
			listProjectHistory: vi.fn().mockImplementation(async (projectId: string) => projectId === "project-a" ? ({
				items: [{
					schema_version: 1,
					run_id: "run-a",
					project_id: "project-a",
					project_url: "https://a.example.com",
					status: "COMPLETED",
					created_at: "2026-09-03T00:00:00Z",
					started_at: "2026-09-03T00:00:01Z",
					finished_at: "2026-09-03T00:00:05Z",
					output_dir: "/work/outputs/run-a",
					last_event_id: 5,
					summary: null,
					error: null,
					instruction: "历史任务指令",
					events: [
						{ schema: "webretriever.run-event/v1", run_id: "run-a", event_id: 1, type: "task.started", occurred_at: "2026-09-03T00:00:01Z", level: "info", task: { task_id: "task-a", task_idx: 0 }, payload: { website_display: "a.example.com" } },
						{ schema: "webretriever.run-event/v1", run_id: "run-a", event_id: 2, type: "task.finished", occurred_at: "2026-09-03T00:00:04Z", level: "info", task: { task_id: "task-a", task_idx: 0 }, payload: { answer: "历史任务回答" } },
						{ schema: "webretriever.run-event/v1", run_id: "run-a", event_id: 3, type: "run.completed", occurred_at: "2026-09-03T00:00:05Z", level: "info", task: null, payload: {} }
					]
				}],
				next_cursor: null
			}) : ({ items: [], next_cursor: null })),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};

		render(<App bridge={readyBridge()} createClient={() => client} />);

		expect(await screen.findByText("历史任务指令")).toBeVisible();
		expect(screen.getByText("历史任务回答")).toBeVisible();
		await user.click(screen.getByRole("button", { name: "项目 项目 B" }));

		expect(await screen.findByText("暂无任务记录")).toBeVisible();
		expect(screen.queryByText("历史任务指令")).not.toBeInTheDocument();
	});

	it("confirms project deletion, removes its local state, and selects the next project", async () => {
		const user = userEvent.setup();
		localStorage.setItem("webAgent.projects", JSON.stringify([
			{ id: "project-a", name: "项目 A", websiteUrl: "https://a.example.com", createdAt: "2026-09-01T00:00:00Z" },
			{ id: "project-b", name: "项目 B", websiteUrl: "https://b.example.com", createdAt: "2026-09-02T00:00:00Z" }
		]));
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn(),
			getRun: vi.fn(),
			deleteProject: vi.fn().mockResolvedValue(undefined),
			listProjectHistory: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};

		render(<App bridge={readyBridge()} createClient={() => client} />);
		await user.click(screen.getByRole("button", { name: "删除项目 项目 A" }));
		expect(screen.getByRole("dialog", { name: "删除项目" })).toBeVisible();
		await user.click(screen.getByRole("button", { name: "确认删除" }));

		expect(client.deleteProject).toHaveBeenCalledWith("project-a");
		expect(screen.queryByRole("button", { name: "项目 项目 A" })).not.toBeInTheDocument();
		expect(screen.getByRole("button", { name: "项目 项目 B" })).toBeVisible();
		expect(screen.getByLabelText("当前项目：项目 B")).toBeVisible();
		expect(JSON.parse(localStorage.getItem("webAgent.projects") ?? "[]")).toEqual([
			{ id: "project-b", name: "项目 B", websiteUrl: "https://b.example.com", createdAt: "2026-09-02T00:00:00Z" }
		]);
	});

	it("includes the current project URL in batch run preflight context", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn().mockResolvedValue({ schema_version: 1, task_count: 1, warnings: [] }),
			createRun: vi.fn(),
			getRun: vi.fn(),
			subscribeToRun: vi.fn()
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "https://example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));
		await openBatchOperations(user);
		await user.type(screen.getByLabelText("任务文件"), "/work/tasks.json");
		await user.type(screen.getByLabelText("输出目录"), "/work/outputs");
		await user.type(screen.getByLabelText("模型配置"), "local-default");
		await user.click(screen.getByRole("button", { name: "预检任务" }));

		expect(await screen.findByText("预检通过：1 个任务")).toBeVisible();
		expect(client.preflight).toHaveBeenCalledWith(expect.objectContaining({ project_url: "https://example.com" }));
	});

	it("continues a task submission after creating the required project", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-8",
				status: "STARTING",
				created_at: "2026-09-04T12:00:00Z",
				snapshot_url: "/api/v1/runs/run-8",
				events_url: "/api/v1/runs/run-8/events"
			}),
			getRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-8",
				status: "RUNNING",
				created_at: "2026-09-04T12:00:00Z",
				started_at: "2026-09-04T12:00:01Z",
				finished_at: null,
				output_dir: "/work/outputs/run-8",
				last_event_id: 1,
				summary: null,
				error: null
			}),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.type(screen.getByLabelText("任务描述"), "整理首页信息");
		await user.click(screen.getByRole("button", { name: "提交任务" }));
		expect(screen.getByRole("dialog", { name: "创建项目" })).toBeVisible();

		await user.type(screen.getByLabelText("网站名称"), "产品官网");
		await user.type(screen.getByLabelText("网站 URL"), "https://example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));

		expect(await screen.findByText("任务已开始，正在打开浏览器…")).toBeVisible();
		expect(client.submitTask).toHaveBeenCalledWith({
			task: "整理首页信息",
			website_url: "https://example.com"
		});
	});

	it("enables run configuration when the sidecar becomes ready after the initial descriptor request", async () => {
		let stateListener: ((status: { state: "READY" }) => void) | undefined;
		const bridge = {
			...readyBridge(),
			getDescriptor: vi.fn()
				.mockRejectedValueOnce(new Error("sidecar is still starting"))
				.mockResolvedValue({
					baseUrl: "http://127.0.0.1:43127",
					bearerToken: "ephemeral-token",
					protocolVersion: 1
				}),
			onBackendStateChanged: vi.fn().mockImplementation(async (listener) => {
				stateListener = listener;
				return () => {};
			})
		};

		render(<App bridge={bridge} />);

		await act(async () => {});
		expect(stateListener).toBeDefined();
		await act(async () => stateListener?.({ state: "READY" }));
		await openBatchOperations(userEvent.setup());

		expect(await screen.findByRole("button", { name: "选择文件" })).toBeEnabled();
	});

	it("shows the backend ready notice once and removes it from the page", async () => {
		vi.useFakeTimers();
		try {
			let stateListener: ((status: { state: "READY" }) => void) | undefined;
			const bridge = {
				...readyBridge(),
				onBackendStateChanged: vi.fn().mockImplementation(async (listener) => {
					stateListener = listener;
					return () => {};
				})
			};

			render(<App bridge={bridge} />);
			await act(async () => {});

			expect(screen.getByText("后端已就绪")).toBeVisible();
			await act(async () => stateListener?.({ state: "READY" }));
			expect(screen.getAllByText("后端已就绪")).toHaveLength(1);

			await act(async () => {
				vi.runOnlyPendingTimers();
			});
			expect(screen.queryByText("后端已就绪")).not.toBeInTheDocument();
		} finally {
			vi.useRealTimers();
		}
	});

	it("does not show a ready notice while the descriptor is still loading", async () => {
		let stateListener: ((status: { state: "READY" }) => void) | undefined;
		const bridge = {
			...readyBridge(),
			getDescriptor: vi.fn().mockImplementation(() => new Promise(() => {})),
			onBackendStateChanged: vi.fn().mockImplementation(async (listener) => {
				stateListener = listener;
				return () => {};
			})
		};

		render(<App bridge={bridge} />);
		await act(async () => stateListener?.({ state: "READY" }));

		expect(screen.queryByText("后端已就绪")).not.toBeInTheDocument();
	});

	it("enables preflight only after the native shell publishes a ready descriptor", async () => {
	const bridge = readyBridge();

    render(<App bridge={bridge} />);

    expect(await screen.findByText("后端已就绪")).toBeVisible();
	    await openBatchOperations(userEvent.setup());
	    expect(screen.getByRole("button", { name: "预检任务" })).toBeEnabled();
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
		await openBatchOperations(user);
		await user.type(screen.getByLabelText("任务文件"), "/work/tasks.json");
		await user.type(screen.getByLabelText("输出目录"), "/work/outputs");
		await user.type(screen.getByLabelText("模型配置"), "local-default");
		await user.click(screen.getByRole("button", { name: "预检任务" }));

		expect(await screen.findByText("预检通过：2 个任务")).toBeVisible();
	});

	it("fills the task input from the native file dialog result", async () => {
		const user = userEvent.setup();
		const bridge = readyBridge();
		bridge.pickTaskFile.mockResolvedValue("/work/selected-tasks.json");

		render(<App bridge={bridge} />);
		await screen.findByText("后端已就绪");
		await openBatchOperations(user);
		await user.click(screen.getByRole("button", { name: "选择文件" }));

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
		await openBatchOperations(user);
		await user.type(screen.getByLabelText("任务文件"), "/work/tasks.json");
		await user.type(screen.getByLabelText("输出目录"), "/work/outputs");
		await user.type(screen.getByLabelText("模型配置"), "local-default");
		expect(screen.getByRole("button", { name: "开始运行" })).toBeDisabled();
		await user.click(screen.getByRole("button", { name: "预检任务" }));
		await screen.findByText("预检通过：1 个任务");
		await user.click(screen.getByRole("button", { name: "开始运行" }));

		expect(await screen.findByText("运行 run-7 已开始")).toBeVisible();
		expect(await screen.findByText("运行状态：FAILED")).toBeVisible();
		expect(await screen.findByText("任务 4：task-4（example.com）")).toBeVisible();
		expect(await screen.findByText("runner_failed")).toBeVisible();
	});

	it("replays a completed task error that is absent from the terminal snapshot", async () => {
		const user = userEvent.setup();
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-timeout",
				status: "STARTING",
				created_at: "2026-09-04T12:00:00Z",
				snapshot_url: "/api/v1/runs/run-timeout",
				events_url: "/api/v1/runs/run-timeout/events"
			}),
			getRun: vi.fn().mockResolvedValue({
				schema_version: 1,
				run_id: "run-timeout",
				status: "COMPLETED",
				created_at: "2026-09-04T12:00:00Z",
				started_at: "2026-09-04T12:00:01Z",
				finished_at: "2026-09-04T12:10:01Z",
				output_dir: "/work/outputs/run-timeout",
				last_event_id: 3,
				summary: null,
				error: null
			}),
			subscribeToRun: vi.fn().mockImplementation(async (_runId, after, onEvent) => {
				if (after === 0) {
					onEvent({
						schema: "webretriever.run-event/v1",
						run_id: "run-timeout",
						event_id: 1,
						type: "task.started",
						occurred_at: "2026-09-04T12:00:01Z",
						level: "info",
						task: { task_id: "task-timeout", task_idx: 0 },
						payload: { website_display: "timeout.example.com" }
					});
					onEvent({
						schema: "webretriever.run-event/v1",
						run_id: "run-timeout",
						event_id: 2,
						type: "task.finished",
						occurred_at: "2026-09-04T12:10:00Z",
						level: "info",
						task: { task_id: "task-timeout", task_idx: 0 },
						payload: { domain_status: "FAIL_MODEL_TIMEOUT", error: "Model request exceeded 0 seconds after up to 5 connection attempts" }
					});
					onEvent({
						schema: "webretriever.run-event/v1",
						run_id: "run-timeout",
						event_id: 3,
						type: "run.completed",
						occurred_at: "2026-09-04T12:10:01Z",
						level: "info",
						task: null,
						payload: { summary: {} }
					});
				}
				return () => {};
			})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "创建项目" }));
		await user.type(screen.getByLabelText("网站名称"), "超时项目");
		await user.type(screen.getByLabelText("网站 URL"), "https://timeout.example.com");
		await user.click(screen.getByRole("button", { name: "确定" }));
		await user.type(screen.getByLabelText("任务描述"), "执行一个可能超时的任务");
		await user.click(screen.getByRole("button", { name: "提交任务" }));

		expect(await screen.findByText("运行状态：COMPLETED")).toBeVisible();
		expect(await screen.findByRole("alert")).toHaveTextContent("Model request exceeded 0 seconds after up to 5 connection attempts");
		expect(client.subscribeToRun).toHaveBeenCalledWith("run-timeout", 0, expect.any(Function));
	});

	it("starts a blank conversation for the project selected by the new-task button", async () => {
		const user = userEvent.setup();
		localStorage.setItem("webAgent.projects", JSON.stringify([
			{ id: "project-a", name: "项目 A", websiteUrl: "https://a.example.com", createdAt: "2026-09-01T00:00:00Z" },
			{ id: "project-b", name: "项目 B", websiteUrl: "https://b.example.com", createdAt: "2026-09-02T00:00:00Z" }
		]));
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn(),
			getRun: vi.fn(),
			listProjectHistory: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await screen.findByRole("button", { name: "项目 项目 A" });
		await user.type(screen.getByLabelText("任务描述"), "尚未提交的草稿");
		await user.click(screen.getByRole("button", { name: "为项目 project-a 新建任务" }));

		expect(screen.getByLabelText("任务描述")).toHaveValue("");
		expect(screen.getByText("暂无任务记录")).toBeVisible();
		await user.click(screen.getByRole("button", { name: "为项目 project-b 新建任务" }));
		expect(screen.getByLabelText("当前项目：项目 B")).toBeVisible();
	});

	it("opens project history from the project row action", async () => {
		const user = userEvent.setup();
		localStorage.setItem("webAgent.projects", JSON.stringify([
			{ id: "project-history", name: "研究网", websiteUrl: "https://research.example.com", createdAt: "2026-09-01T00:00:00Z" }
		]));
		const client = {
			preflight: vi.fn(),
			createRun: vi.fn(),
			submitTask: vi.fn(),
			getRun: vi.fn(),
			listProjectHistory: vi.fn().mockResolvedValue({
				items: [{
					schema_version: 1,
					run_id: "run-history",
					project_id: "project-history",
					project_url: "https://research.example.com",
					status: "COMPLETED",
					created_at: "2026-09-04T12:00:00Z",
					started_at: "2026-09-04T12:00:01Z",
					finished_at: "2026-09-04T12:00:05Z",
					output_dir: "/work/outputs/run-history",
					last_event_id: 3,
					summary: null,
					error: null,
					instruction: "整理研究网首页",
					events: []
				}],
				next_cursor: null
			}),
			subscribeToRun: vi.fn().mockResolvedValue(() => {})
		};
		render(<App bridge={readyBridge()} createClient={() => client} />);

		await user.click(screen.getByRole("button", { name: "查看项目 研究网 的历史任务" }));

		const dialog = await screen.findByRole("dialog", { name: "项目历史任务：研究网" });
		expect(dialog).toBeVisible();
		expect(within(dialog).getByText("整理研究网首页")).toBeVisible();
		expect(client.listProjectHistory).toHaveBeenCalledWith("project-history", expect.objectContaining({ limit: 100 }));
		await user.click(screen.getByRole("button", { name: "关闭" }));
		expect(screen.queryByRole("dialog", { name: "项目历史任务：研究网" })).not.toBeInTheDocument();
	});
});
