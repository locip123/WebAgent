import { useEffect, useRef, useState } from "react";
import type { BackendDescriptor, BackendStatus, DesktopBridge } from "./bridge";
import {
  ControlPlaneClient,
  LocalControlPlaneProblem,
  localizeProblem,
  type ControlPlaneApi,
  type PreflightResult,
  type RunAccepted,
  type RunSpec
} from "./api/controlPlaneClient";
import { applyRunEvent, createRunProjection, type RunProjection, type RunSnapshot } from "./runProjection";

export interface AppProps {
  bridge: DesktopBridge;
  createClient?: (descriptor: BackendDescriptor) => ControlPlaneApi;
}

const backendLabels: Record<BackendStatus["state"], string> = {
  STARTING: "正在启动后端",
  READY: "后端已就绪",
  RESTARTING: "正在重启后端",
  DRAINING: "正在停止后端",
  STOPPED: "后端已停止",
  FAILED: "后端启动失败"
};

export function App({ bridge, createClient = (readyDescriptor) => new ControlPlaneClient(readyDescriptor) }: AppProps) {
  const [descriptor, setDescriptor] = useState<BackendDescriptor | null>(null);
  const [backend, setBackend] = useState<BackendStatus>({ state: "STARTING" });
  const [inputPath, setInputPath] = useState("");
  const [outputRoot, setOutputRoot] = useState("");
  const [profileId, setProfileId] = useState("");
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<RunAccepted | null>(null);
  const [runSnapshot, setRunSnapshot] = useState<RunSnapshot | null>(null);
  const [projection, setProjection] = useState<RunProjection | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const unsubscribeRunEvents = useRef<(() => void) | null>(null);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    void bridge.onBackendStateChanged((status) => {
      if (disposed) return;
      setBackend(status);
      if (status.state !== "READY") setDescriptor(null);
    }).then((cleanup) => {
      if (disposed) cleanup();
      else unlisten = cleanup;
    });

    void bridge.getDescriptor().then(
      (readyDescriptor) => {
        if (disposed) return;
        setDescriptor(readyDescriptor);
        setBackend({ state: "READY" });
      },
      () => {
        if (!disposed) setBackend({ state: "FAILED" });
      }
    );

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [bridge]);

  useEffect(() => () => unsubscribeRunEvents.current?.(), []);

  const ready = descriptor !== null && backend.state === "READY";

  function currentSpec(): RunSpec {
    return {
      schema_version: 1,
      input_path: inputPath,
      output_root: outputRoot,
      model: { profile_id: profileId },
      browser: { mode: "local", headed: false },
      limits: {
        max_concurrency: 1,
        max_steps: 100,
        model_timeout_seconds: 180,
        task_timeout_seconds: 60
      },
      selection: {}
    };
  }

  function changeInput(value: string, change: (nextValue: string) => void) {
    change(value);
    setPreflight(null);
    setActiveRun(null);
    setRunSnapshot(null);
    setProjection(null);
  }

  async function preflightRun() {
    if (!descriptor) return;
    setPreflight(null);
    setPreflightError(null);
    try {
      setPreflight(await createClient(descriptor).preflight(currentSpec()));
    } catch (error) {
      setPreflightError(localMessage(error, "预检失败，请检查任务文件、输出目录和模型配置。"));
    }
  }

  async function startRun() {
    if (!descriptor || !preflight) return;
    setRunError(null);
    try {
		unsubscribeRunEvents.current?.();
      const client = createClient(descriptor);
      const accepted = await client.createRun(currentSpec());
      setActiveRun(accepted);
		const snapshot = await client.getRun(accepted.run_id);
		setRunSnapshot(snapshot);
		setProjection(createRunProjection(snapshot));
		unsubscribeRunEvents.current = await client.subscribeToRun(accepted.run_id, snapshot.last_event_id, (event) => {
			setProjection((current) => (current ? applyRunEvent(current, event) : current));
		});
    } catch (error) {
      setRunError(localMessage(error, "无法开始运行。请处理预检问题或稍后重试。"));
    }
  }

  async function pickTaskFile() {
    const selected = await bridge.pickTaskFile();
    if (selected) changeInput(selected, setInputPath);
  }

  async function pickOutputDirectory() {
    const selected = await bridge.pickOutputDirectory();
    if (selected) changeInput(selected, setOutputRoot);
  }

  return (
    <main className="desktop-workspace">
      <header>
        <h1>WebRetriever</h1>
        <p aria-live="polite" role="status">{backendLabels[backend.state]}</p>
      </header>

      {backend.state === "FAILED" && (
        <section role="alert">
          <p>{backend.message ?? "无法连接本地执行后端。"}</p>
          <button type="button" onClick={() => void bridge.retrySidecar()}>重试后端</button>
        </section>
      )}

      <section aria-label="运行配置">
        <h2>运行配置</h2>
        <label>
          任务文件
          <input aria-label="任务文件" type="text" value={inputPath} onChange={(event) => changeInput(event.target.value, setInputPath)} disabled={!ready} />
        </label>
        <button type="button" disabled={!ready} onClick={() => void pickTaskFile()}>选择任务文件</button>
        <label>
          输出目录
          <input aria-label="输出目录" type="text" value={outputRoot} onChange={(event) => changeInput(event.target.value, setOutputRoot)} disabled={!ready} />
        </label>
        <button type="button" disabled={!ready} onClick={() => void pickOutputDirectory()}>选择输出目录</button>
        <label>
          模型配置
          <input aria-label="模型配置" type="text" value={profileId} onChange={(event) => changeInput(event.target.value, setProfileId)} disabled={!ready} />
        </label>
        <div>
          <button type="button" disabled={!ready} onClick={() => void preflightRun()}>预检</button>
          <button type="button" disabled={!ready || !preflight} onClick={() => void startRun()}>开始运行</button>
        </div>
        {preflight && <p role="status">预检通过：{preflight.task_count} 个任务</p>}
        {preflightError && <p role="alert">{preflightError}</p>}
        {activeRun && <p role="status">运行 {activeRun.run_id} 已开始</p>}
        {runSnapshot && <p role="status">运行状态：{projection?.snapshot.status ?? runSnapshot.status}</p>}
        {runError && <p role="alert">{runError}</p>}
      </section>

      {projection && (
        <section aria-label="运行详情">
          <h2>运行详情</h2>
          <h3>任务</h3>
          {Object.values(projection.tasks).length === 0 ? <p>尚未开始任务</p> : (
            <ul>
              {Object.values(projection.tasks).map((task) => (
                <li key={task.taskId}>任务 {task.taskIdx}：{task.taskId}（{task.website}）{task.phase ? ` — ${task.phase}` : ""}</li>
              ))}
            </ul>
          )}
          <h3>工件</h3>
          {projection.artifacts.length === 0 ? <p>暂无工件</p> : (
            <ul>
              {projection.artifacts.map((artifact) => (
                <li key={artifact.artifactId}>{artifact.kind}（{artifact.mimeType}，{artifact.size} bytes）</li>
              ))}
            </ul>
          )}
          <h3>错误</h3>
          {projection.errors.length === 0 ? <p>暂无错误</p> : (
            <ul>
              {projection.errors.map((error) => (
                <li key={error.eventId}>{error.code}</li>
              ))}
            </ul>
          )}
        </section>
      )}
    </main>
  );
}

function localMessage(error: unknown, fallback: string): string {
  return error instanceof LocalControlPlaneProblem ? localizeProblem(error) : fallback;
}
