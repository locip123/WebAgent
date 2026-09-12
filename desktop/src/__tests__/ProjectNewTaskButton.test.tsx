import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FormEvent } from "react";
import { describe, expect, it, vi } from "vitest";
import { ProjectNewTaskButton } from "../ProjectNewTaskButton";

describe("ProjectNewTaskButton", () => {
  it("renders an accessible new-task button and creates a task for its project", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();

    render(<ProjectNewTaskButton projectId="project-123" onCreate={onCreate} />);

    const button = screen.getByRole("button", { name: "为项目 project-123 新建任务" });
    expect(button).toHaveTextContent("新建任务");
    expect(button).toHaveAttribute("type", "button");

    await user.click(button);

    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(onCreate).toHaveBeenCalledWith("project-123");
  });

  it("does not submit an enclosing form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((event: FormEvent<HTMLFormElement>) => event.preventDefault());

    render(
      <form onSubmit={onSubmit}>
        <ProjectNewTaskButton projectId="project-123" onCreate={vi.fn()} />
      </form>
    );

    await user.click(screen.getByRole("button", { name: "为项目 project-123 新建任务" }));

    expect(onSubmit).not.toHaveBeenCalled();
  });
});
