import { useEffect, useState } from "react";
import type { ProjectHistoryItem, ProjectHistoryPage } from "./api/controlPlaneClient";

export interface ProjectHistoryDialogProps {
  projectId: string;
  projectName: string;
  open: boolean;
  loadHistory: (projectId: string) => Promise<ProjectHistoryPage>;
  onClose: () => void;
}

export function ProjectHistoryDialog({
  projectId,
  projectName,
  open,
  loadHistory,
  onClose
}: ProjectHistoryDialogProps) {
  const [items, setItems] = useState<ProjectHistoryItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;

    let isCurrent = true;
    setItems([]);
    setError(null);
    setIsLoading(true);

    void loadHistory(projectId).then(
      (page) => {
        if (!isCurrent) return;
        setItems(page.items);
        setIsLoading(false);
      },
      (reason: unknown) => {
        if (!isCurrent) return;
        setError(reason instanceof Error && reason.message ? reason.message : "未知错误");
        setIsLoading(false);
      }
    );

    return () => {
      isCurrent = false;
    };
  }, [loadHistory, open, projectId]);

  if (!open) return null;

  return (
    <div className="project-dialog-backdrop">
      <section className="project-dialog project-history-dialog" role="dialog" aria-modal="true" aria-labelledby="project-history-title">
        <div className="project-history-dialog__header">
          <h2 id="project-history-title">项目历史任务：{projectName}</h2>
          <button className="button button--secondary" type="button" onClick={onClose}>
            关闭
          </button>
        </div>

        {isLoading ? (
          <p role="status">正在加载历史任务…</p>
        ) : error ? (
          <p role="alert">加载历史任务失败：{error}</p>
        ) : items.length === 0 ? (
          <p>暂无历史任务</p>
        ) : (
          <ul aria-label="历史任务列表">
            {items.map((item) => (
              <li key={item.run_id}>
                <article>
                  <h3>任务指令</h3>
                  <p>{item.instruction ?? "未提供任务指令"}</p>
                  <dl>
                    <div>
                      <dt>状态</dt>
                      <dd>{item.status}</dd>
                    </div>
                    <div>
                      <dt>创建时间</dt>
                      <dd>{item.created_at}</dd>
                    </div>
                  </dl>
                </article>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
