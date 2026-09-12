import type { ReactNode } from "react";

export interface ProjectNewTaskButtonProps {
  projectId: string;
  onCreate: (projectId: string) => void;
}

export function ProjectNewTaskButton({ projectId, onCreate }: ProjectNewTaskButtonProps): ReactNode {
  return (
    <button
      type="button"
      aria-label={`为项目 ${projectId} 新建任务`}
      onClick={() => onCreate(projectId)}
    >
      新建任务
    </button>
  );
}
