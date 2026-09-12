import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProjectHistoryItem, ProjectHistoryPage } from "../api/controlPlaneClient";
import { ProjectHistoryDialog } from "../ProjectHistoryDialog";

const historyItem: ProjectHistoryItem = {
  schema_version: 1,
  run_id: "run-1",
  project_id: "project-1",
  project_url: "https://example.com",
  status: "COMPLETED",
  created_at: "2026-09-04T12:00:00Z",
  started_at: "2026-09-04T12:00:01Z",
  finished_at: "2026-09-04T12:00:05Z",
  output_dir: "/work/outputs/run-1",
  last_event_id: 3,
  summary: null,
  error: null,
  instruction: "整理首页信息",
  events: []
};

function historyPage(items: ProjectHistoryItem[] = [historyItem]): ProjectHistoryPage {
  return { schema_version: 1, items, next_cursor: null };
}

describe("ProjectHistoryDialog", () => {
  it("does not load or render while closed", () => {
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>();

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open={false}
        loadHistory={loadHistory}
        onClose={vi.fn()}
      />
    );

    expect(loadHistory).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens by loading the project's history and showing task details", async () => {
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>().mockResolvedValue(historyPage());

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open
        loadHistory={loadHistory}
        onClose={vi.fn()}
      />
    );

    expect(loadHistory).toHaveBeenCalledWith("project-1");
    expect(await screen.findByRole("dialog", { name: "项目历史任务：研究网" })).toBeVisible();
    expect(screen.getByText("整理首页信息")).toBeVisible();
    expect(screen.getByText("状态")).toBeVisible();
    expect(screen.getByText("COMPLETED")).toBeVisible();
    expect(screen.getByText("创建时间")).toBeVisible();
    expect(screen.getByText("2026-09-04T12:00:00Z")).toBeVisible();
  });

  it("shows a loading state while history is being fetched", () => {
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>().mockReturnValue(
      new Promise(() => {})
    );

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open
        loadHistory={loadHistory}
        onClose={vi.fn()}
      />
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在加载历史任务");
  });

  it("shows an empty state when the project has no history", async () => {
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>().mockResolvedValue(historyPage([]));

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open
        loadHistory={loadHistory}
        onClose={vi.fn()}
      />
    );

    expect(await screen.findByText("暂无历史任务")).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows the request error when history cannot be loaded", async () => {
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>().mockRejectedValue(
      new Error("网络暂时不可用")
    );

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open
        loadHistory={loadHistory}
        onClose={vi.fn()}
      />
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("加载历史任务失败：网络暂时不可用");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("notifies the parent when the dialog is closed", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const loadHistory = vi.fn<(projectId: string) => Promise<ProjectHistoryPage>>().mockResolvedValue(historyPage());

    render(
      <ProjectHistoryDialog
        projectId="project-1"
        projectName="研究网"
        open
        loadHistory={loadHistory}
        onClose={onClose}
      />
    );

    await user.click(screen.getByRole("button", { name: "关闭" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
