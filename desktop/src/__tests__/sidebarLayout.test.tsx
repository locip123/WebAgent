import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

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

describe("项目栏响应式布局", () => {
  beforeEach(() => localStorage.clear());

  it("把项目栏限制在可用范围内，并让内容区占用剩余视口宽度", () => {
    localStorage.setItem("webAgent.projects", JSON.stringify([
      {
        id: "project-layout",
        name: "一个很长的项目名称",
        websiteUrl: "https://a-very-long-project-url.example.com/workspace",
        createdAt: "2026-09-10T00:00:00Z"
      }
    ]));

    render(<App bridge={readyBridge()} />);

    const sidebar = screen.getByRole("complementary");
    const main = screen.getByRole("main");
    expect(sidebar).toContainElement(screen.getByRole("button", { name: "项目 一个很长的项目名称" }));
    expect(main).toContainElement(screen.getByRole("region", { name: "我的工作区" }));

    expect(styles).toMatch(/\.app-shell \{[^}]*height: 100dvh;[^}]*overflow: hidden;/);
    expect(styles).toMatch(/\.sidebar \{[^}]*flex: 0 0 clamp\(280px, 26vw, 360px\);[^}]*width: clamp\(280px, 26vw, 360px\);[^}]*min-width: 0;[^}]*min-height: 0;[^}]*overflow-x: hidden;[^}]*overflow-y: auto;/);
    expect(styles).toMatch(/\.app-main \{[^}]*min-width: 0;[^}]*min-height: 0;[^}]*overflow: hidden;/);
    expect(styles).toMatch(/\.content-area \{[^}]*min-width: 0;[^}]*overflow: auto;/);
    expect(styles).toMatch(/\.sidebar-section \{[^}]*flex: 1 1 auto;[^}]*min-width: 0;[^}]*min-height: 0;[^}]*overflow: auto;/);
    expect(styles).toMatch(/\.project-entry \{[^}]*min-width: 0;/);
    expect(styles).toMatch(/\.project-link \{[^}]*flex: 1 1 auto;[^}]*width: auto;[^}]*min-width: 0;/);
    expect(styles).toMatch(/@media \(max-width: 720px\) \{[\s\S]*\.project-entry-actions \{[^}]*position: absolute;[^}]*pointer-events: none;[\s\S]*\.project-entry:hover \.project-entry-actions, \.project-entry:focus-within \.project-entry-actions \{[^}]*pointer-events: auto;/);
    expect(styles).toContain('.project-entry-actions .project-history-button::after { content: "历"; }');
    expect(styles).toContain('.project-entry-actions .project-delete-button::after { content: "删"; }');
  });

  it("makes each project's sidebar actions visible, grouped, and operable at narrow desktop widths", () => {
    localStorage.setItem("webAgent.projects", JSON.stringify([
      {
        id: "project-layout-actions",
        name: "Layout Project",
        websiteUrl: "https://a-very-long-project-url.example.com/workspace/with/more/path",
        createdAt: "2026-09-10T00:00:00Z"
      }
    ]));

    render(
      <>
        <style>{styles}</style>
        <App bridge={readyBridge()} />
      </>
    );

    const newTaskButton = screen.getByRole("button", { name: /project-layout-actions/ });
    const actionGroup = newTaskButton.parentElement;
    expect(actionGroup).not.toBeNull();

    const historyButton = within(actionGroup as HTMLElement).getByRole("button", { name: "查看项目 Layout Project 的历史任务" });
    const deleteButton = within(actionGroup as HTMLElement).getByRole("button", { name: /删除项目 Layout Project/ });
    expect(newTaskButton).toBeVisible();
    expect(historyButton).toBeVisible();
    expect(deleteButton).toBeVisible();
    expect(within(actionGroup as HTMLElement).getAllByRole("button")).toHaveLength(3);

    expect(getComputedStyle(actionGroup as HTMLElement).flexWrap).toBe("nowrap");
    expect(getComputedStyle(actionGroup as HTMLElement).flexShrink).toBe("0");
    expect(getComputedStyle(actionGroup as HTMLElement).gap).toBe("4px");
    for (const button of [newTaskButton, historyButton, deleteButton]) {
      expect(getComputedStyle(button).minHeight).toBe("30px");
      expect(getComputedStyle(button).pointerEvents).not.toBe("none");
    }
  });
});
