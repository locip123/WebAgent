import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { BackendDescriptor, BackendStatus, DesktopBridge } from "./bridge";
import {
  ControlPlaneClient,
  LocalControlPlaneProblem,
  localizeProblem,
  type ControlPlaneApi,
  type AccountProfile,
	 type ModelServiceSummary,
	 type ModelServiceInput as ModelServiceRequest,
	 type ModelServiceTestResult,
  type PreflightResult,
  type RunAccepted,
  type RunSpec
} from "./api/controlPlaneClient";
import { applyRunEvent, createRunProjection, type RunEvent, type RunProjection, type RunSnapshot } from "./runProjection";
import {
  createProject,
  loadProjects,
  saveProjects,
  validateProjectInput,
  type Project,
  type ProjectInput,
  type ProjectValidationErrors
} from "./projectStore";
import { ProjectHistoryDialog } from "./ProjectHistoryDialog";
import { ProjectNewTaskButton } from "./ProjectNewTaskButton";

type AppView = "home" | "settings" | "help" | "batch";

type ModelServiceForm = {
  name: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  responseMode: "responses" | "chat-completions";
};

const defaultModelServiceForm: ModelServiceForm = {
	name: "response",
  baseUrl: "",
  apiKey: "",
	model: "gpt-5.5",
	responseMode: "responses"
};

const defaultAccountProfile: AccountProfile = {
  name: "林晓宇",
  email: "",
  age: null,
  work: "",
  organization: ""
};

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

const backendReadyNoticeDuration = 4000;

const workspaceTaskStartedNotice = "任务已开始，正在打开浏览器…";

const WORKSPACE_RUNS_STORAGE_KEY = "webAgent.projectWorkspaceRuns";

type WorkspaceRunReference = {
  runId: string;
  instruction: string;
};

type WorkspaceRunReferences = Record<string, WorkspaceRunReference>;

type WorkspaceRunHistoryItem = RunSnapshot & {
  project_id?: string | null;
  project_url?: string | null;
  projectId?: string | null;
  projectUrl?: string | null;
  instruction?: string | null;
  task?: string;
  events?: RunEvent[];
};

type WorkspaceRunHistoryResult =
  | { items: WorkspaceRunHistoryItem[]; next_cursor?: string | null }
  | WorkspaceRunHistoryItem[];

type WorkspaceHistoryClient = ControlPlaneApi & {
  listRuns?: () => Promise<WorkspaceRunHistoryResult>;
  listProjectHistory?: (
    projectId: string,
    query?: { cursor?: string; limit?: number }
  ) => Promise<{ items: WorkspaceRunHistoryItem[]; next_cursor?: string | null }>;
  getProjectHistory?: (
    projectId: string,
    query?: { cursor?: string; limit?: number }
  ) => Promise<{ items: WorkspaceRunHistoryItem[]; next_cursor?: string | null }>;
};

type WorkspaceRunRecord = {
  runId: string;
  instruction: string;
  accepted: RunAccepted | null;
  projection: RunProjection;
  isCancelling: boolean;
};

function loadWorkspaceRunReferences(): WorkspaceRunReferences {
  if (typeof window === "undefined") return {};
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(WORKSPACE_RUNS_STORAGE_KEY) ?? "{}");
    if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value).filter(([, reference]) => isWorkspaceRunReference(reference))
    ) as WorkspaceRunReferences;
  } catch {
    return {};
  }
}

function saveWorkspaceRunReferences(references: WorkspaceRunReferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(WORKSPACE_RUNS_STORAGE_KEY, JSON.stringify(references));
  } catch {
    // A desktop session can still work when browser storage is unavailable.
  }
}

function isWorkspaceRunReference(value: unknown): value is WorkspaceRunReference {
  if (typeof value !== "object" || value === null) return false;
  const reference = value as Partial<WorkspaceRunReference>;
  return typeof reference.runId === "string" && typeof reference.instruction === "string";
}

function workspaceHistoryItems(result: WorkspaceRunHistoryResult): WorkspaceRunHistoryItem[] {
  return Array.isArray(result) ? result : result.items;
}

function historyItemForProject(
  items: WorkspaceRunHistoryItem[],
  project: Project
): WorkspaceRunHistoryItem | undefined {
  return items.find((item) =>
    item.project_id === project.id ||
    item.projectId === project.id ||
    item.project_url === project.websiteUrl ||
    item.projectUrl === project.websiteUrl
  );
}

function historyItemInstruction(item: WorkspaceRunHistoryItem): string {
  const value = item as WorkspaceRunHistoryItem & { instruction?: unknown; task?: unknown };
  if (typeof value.instruction === "string" && value.instruction.trim()) return value.instruction;
  if (typeof value.task === "string" && value.task.trim()) return value.task;
  return "最近一次任务";
}

function isTerminalRunStatus(status: RunSnapshot["status"] | undefined): boolean {
  return status === "COMPLETED" || status === "CANCELLED" || status === "FAILED" || status === "INTERRUPTED";
}

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
  const [view, setView] = useState<AppView>("home");
  const [taskPrompt, setTaskPrompt] = useState("");
  const [promptNotice, setPromptNotice] = useState<string | null>(null);
  const [isBackendReadyNoticeVisible, setIsBackendReadyNoticeVisible] = useState(false);
  const [workspaceRuns, setWorkspaceRuns] = useState<Record<string, WorkspaceRunRecord>>({});
  const [projects, setProjects] = useState<Project[]>(() => loadProjects());
  const [currentProject, setCurrentProject] = useState<Project | null>(() => loadProjects()[0] ?? null);
  const [historyProject, setHistoryProject] = useState<Project | null>(null);
  const [isProjectDialogOpen, setIsProjectDialogOpen] = useState(false);
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);
  const [projectDeleteError, setProjectDeleteError] = useState<string | null>(null);
  const [isDeletingProject, setIsDeletingProject] = useState(false);
  const [projectForm, setProjectForm] = useState<ProjectInput>({ name: "", websiteUrl: "" });
  const [projectErrors, setProjectErrors] = useState<ProjectValidationErrors>({});
	const [accountProfile, setAccountProfile] = useState<AccountProfile>(defaultAccountProfile);
	const [accountProfileForm, setAccountProfileForm] = useState<AccountProfile>(defaultAccountProfile);
	const [isAccountDialogOpen, setIsAccountDialogOpen] = useState(false);
	const [accountProfileError, setAccountProfileError] = useState<string | null>(null);
	const [isSavingAccountProfile, setIsSavingAccountProfile] = useState(false);
	const isAccountProfileFormDirty = useRef(false);
  const [isModelServiceDialogOpen, setIsModelServiceDialogOpen] = useState(false);
  const [modelServiceForm, setModelServiceForm] = useState<ModelServiceForm>(defaultModelServiceForm);
  const [modelServices, setModelServices] = useState<ModelServiceSummary[]>([]);
  const [modelServiceTestResult, setModelServiceTestResult] = useState<string | null>(null);
	const [modelServiceError, setModelServiceError] = useState<string | null>(null);
	const [isSavingModelService, setIsSavingModelService] = useState(false);
	const [isTestingModelService, setIsTestingModelService] = useState(false);
  const unsubscribeRunEvents = useRef<(() => void) | null>(null);
  const workspaceRunReferences = useRef<WorkspaceRunReferences>(loadWorkspaceRunReferences());
  const workspaceRunIds = useRef<Record<string, string>>({});
  const workspaceSubscriptions = useRef(new Map<string, () => void>());
  const restoredWorkspaceProjects = useRef(new Set<string>());
  const projectRegistrations = useRef(new Map<string, Promise<void>>());
  const backendReadyNoticeShown = useRef(false);

  function showBackendReadyNoticeOnce() {
    if (backendReadyNoticeShown.current) return;
    backendReadyNoticeShown.current = true;
    setIsBackendReadyNoticeVisible(true);
  }

  useEffect(() => {
    if (!isBackendReadyNoticeVisible) return;
    const timeout = window.setTimeout(() => setIsBackendReadyNoticeVisible(false), backendReadyNoticeDuration);
    return () => window.clearTimeout(timeout);
  }, [isBackendReadyNoticeVisible]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    let descriptorRequest = 0;

    function loadDescriptor() {
      const request = ++descriptorRequest;
      void bridge.getDescriptor().then(
        (readyDescriptor) => {
          if (disposed || request !== descriptorRequest) return;
          setDescriptor(readyDescriptor);
          setBackend({ state: "READY" });
          showBackendReadyNoticeOnce();
        },
        () => {
          if (disposed || request !== descriptorRequest) return;
          setDescriptor(null);
          setBackend({ state: "FAILED" });
        }
      );
    }

    void bridge.onBackendStateChanged((status) => {
      if (disposed) return;
      setBackend(status);
      if (status.state === "READY") loadDescriptor();
      else {
        descriptorRequest += 1;
        setDescriptor(null);
      }
    }).then((cleanup) => {
      if (disposed) cleanup();
      else unlisten = cleanup;
    });

    loadDescriptor();

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [bridge]);

  useEffect(() => () => unsubscribeRunEvents.current?.(), []);

  useEffect(() => () => {
    workspaceSubscriptions.current.forEach((unsubscribe) => unsubscribe());
    workspaceSubscriptions.current.clear();
  }, []);

	useEffect(() => {
		if (!descriptor) return;
		const client = createClient(descriptor);
		if (!client.getAccountProfile) return;
		void client.getAccountProfile().then(
			(profile) => {
				setAccountProfile(profile);
				if (!isAccountProfileFormDirty.current) setAccountProfileForm(profile);
			},
			() => undefined
		);
  }, [descriptor]);

  const ready = descriptor !== null && backend.state === "READY";
  const currentWorkspaceRun = currentProject ? workspaceRuns[currentProject.id] ?? null : null;
  const currentWorkspaceRunStatus = currentWorkspaceRun?.projection.snapshot.status;
  const currentWorkspaceRunError = currentWorkspaceRunStatus === "COMPLETED"
    ? readableRunError(currentWorkspaceRun?.projection.snapshot.error)
      ?? [...(currentWorkspaceRun?.projection.errors ?? [])].reverse().find((error) => error.message)?.message
      ?? null
    : null;

  useEffect(() => {
    if (isTerminalRunStatus(currentWorkspaceRunStatus)) {
      setPromptNotice((current) => current === workspaceTaskStartedNotice ? null : current);
    }
  }, [promptNotice, currentWorkspaceRunStatus]);

  useEffect(() => {
    if (!descriptor || !currentProject || restoredWorkspaceProjects.current.has(currentProject.id)) return;
    const project = currentProject;
    restoredWorkspaceProjects.current.add(project.id);
    const client = createClient(descriptor) as WorkspaceHistoryClient;

    void (async () => {
      let reference = workspaceRunReferences.current[project.id];
      const readProjectHistory = client.listProjectHistory ?? client.getProjectHistory;
      if (!reference && readProjectHistory) {
        try {
          const result = await readProjectHistory(project.id, { limit: 1 });
          const item = result.items[0];
          if (item) {
            const instruction = historyItemInstruction(item);
            reference = { runId: item.run_id, instruction };
            rememberWorkspaceRun(project.id, reference);
            await restoreWorkspaceHistoryItem(project.id, reference, item, client);
            return;
          }
        } catch {
          // A missing project history is treated as an empty workspace.
        }
      }
      if (!reference && client.listRuns) {
        try {
          const result = await client.listRuns();
          const item = historyItemForProject(workspaceHistoryItems(result), project);
          const instruction = item?.instruction ?? item?.task;
          if (item && typeof instruction === "string" && instruction.trim()) {
            reference = { runId: item.run_id, instruction };
            rememberWorkspaceRun(project.id, reference);
          }
        } catch {
          // Historical workspace state is optional; a list failure should not block the composer.
        }
      }
      if (!reference || workspaceRunIds.current[project.id]) return;

      try {
        const snapshot = await client.getRun(reference.runId);
        if (workspaceRunIds.current[project.id]) return;
        const projection = createRunProjection({ ...snapshot, last_event_id: 0 });
        workspaceRunIds.current[project.id] = reference.runId;
        setWorkspaceRuns((current) => current[project.id] ? current : {
          ...current,
          [project.id]: {
            runId: reference.runId,
            instruction: reference.instruction,
            accepted: null,
            projection,
            isCancelling: false
          }
        });
        await attachWorkspaceSubscription(project.id, reference.runId, client, 0);
      } catch {
        // A stale local reference is treated as no history until a new task is submitted.
      }
    })();
  }, [createClient, currentProject, descriptor]);

  async function restoreWorkspaceHistoryItem(
    projectId: string,
    reference: WorkspaceRunReference,
    item: WorkspaceRunHistoryItem,
    client: ControlPlaneApi
  ): Promise<void> {
    if (workspaceRunIds.current[projectId]) return;
    const projection = (item.events ?? []).reduce(
      (current, event) => applyRunEvent(current, event),
      createRunProjection({ ...item, last_event_id: 0 })
    );
    workspaceRunIds.current[projectId] = reference.runId;
    setWorkspaceRuns((current) => current[projectId] ? current : {
      ...current,
      [projectId]: {
        runId: reference.runId,
        instruction: reference.instruction,
        accepted: null,
        projection,
        isCancelling: projection.snapshot.status === "CANCELLING"
      }
    });
    if (!isTerminalRunStatus(projection.snapshot.status)) {
      await attachWorkspaceSubscription(projectId, reference.runId, client, projection.snapshot.last_event_id);
    }
  }

  function rememberWorkspaceRun(projectId: string, reference: WorkspaceRunReference): void {
    const next = { ...workspaceRunReferences.current, [projectId]: reference };
    workspaceRunReferences.current = next;
    saveWorkspaceRunReferences(next);
  }

  async function attachWorkspaceSubscription(
    projectId: string,
    runId: string,
    client: ControlPlaneApi,
    after: number
  ): Promise<void> {
    const unsubscribe = await client.subscribeToRun(runId, after, (event) => {
      setWorkspaceRuns((current) => {
        const record = current[projectId];
        if (!record || record.runId !== runId) return current;
        const projection = applyRunEvent(record.projection, event);
        if (projection === record.projection) return current;
        return {
          ...current,
          [projectId]: {
            ...record,
            projection,
            isCancelling: isTerminalRunStatus(projection.snapshot.status) ? false : record.isCancelling
          }
        };
      });
    });
    if (workspaceRunIds.current[projectId] !== runId) {
      unsubscribe();
      return;
    }
    workspaceSubscriptions.current.get(projectId)?.();
    workspaceSubscriptions.current.set(projectId, unsubscribe);
  }

  const loadProjectHistory = useCallback((projectId: string) => {
    if (!descriptor) return Promise.reject(new Error("本地后端尚未就绪"));
    const client = createClient(descriptor) as WorkspaceHistoryClient;
    if (client.listProjectHistory) return client.listProjectHistory(projectId, { limit: 100 });
    if (client.getProjectHistory) return client.getProjectHistory(projectId, { limit: 100 });
    return Promise.reject(new Error("历史任务服务暂不可用"));
  }, [createClient, descriptor]);

  function currentSpec(): RunSpec {
    return {
      schema_version: 1,
      input_path: inputPath,
      output_root: outputRoot,
      project_id: currentProject?.id,
      project_url: currentProject?.websiteUrl,
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

  async function submitWorkspaceTask(task: string, project: Project) {
    if (!descriptor) {
      setPromptNotice("本地后端尚未就绪，请稍后重试。");
      return;
    }
    const client = createClient(descriptor);
    if (!client.submitTask) {
      setPromptNotice("任务提交服务暂不可用，请稍后重试。");
      return;
    }
    setPromptNotice("正在提交任务…");
    try {
      workspaceSubscriptions.current.get(project.id)?.();
      workspaceSubscriptions.current.delete(project.id);
      const submission = {
        task,
        website_url: project.websiteUrl,
        ...(client.registerProject || client.listProjectHistory || client.getProjectHistory || (client as WorkspaceHistoryClient).listRuns
          ? { project_id: project.id }
          : {})
      };
      const accepted = await client.submitTask(submission);
      const snapshot = await client.getRun(accepted.run_id);
      const record: WorkspaceRunRecord = {
        runId: accepted.run_id,
        instruction: task,
        accepted,
			projection: createRunProjection({ ...snapshot, last_event_id: 0 }),
        isCancelling: false
      };
      workspaceRunIds.current[project.id] = accepted.run_id;
      setWorkspaceRuns((current) => ({ ...current, [project.id]: record }));
      rememberWorkspaceRun(project.id, { runId: accepted.run_id, instruction: task });
		await attachWorkspaceSubscription(project.id, accepted.run_id, client, 0);
      setTaskPrompt("");
      setPromptNotice(workspaceTaskStartedNotice);
    } catch (error) {
      setPromptNotice(localMessage(error, "无法提交任务，请稍后重试。"));
    }
  }

  async function cancelWorkspaceTask() {
    const project = currentProject;
    const record = currentWorkspaceRun;
    if (!descriptor || !project || !record || record.isCancelling) return;
    const client = createClient(descriptor);
    if (!client.cancelRun) {
      setPromptNotice("当前任务暂不支持停止。");
      return;
    }
    setWorkspaceRuns((current) => {
      const currentRecord = current[project.id];
      if (!currentRecord || currentRecord.runId !== record.runId) return current;
      return { ...current, [project.id]: { ...currentRecord, isCancelling: true } };
    });
    setPromptNotice("正在停止任务…");
    try {
      const result = await client.cancelRun(record.runId);
      setWorkspaceRuns((current) => {
        const currentRecord = current[project.id];
        if (!currentRecord || currentRecord.runId !== record.runId) return current;
        return {
          ...current,
          [project.id]: {
            ...currentRecord,
            isCancelling: result.cancel_applied,
            projection: {
              ...currentRecord.projection,
              snapshot: { ...currentRecord.projection.snapshot, status: result.status }
            }
          }
        };
      });
      if (!result.cancel_applied) setPromptNotice("任务已经结束。");
    } catch (error) {
      setWorkspaceRuns((current) => {
        const currentRecord = current[project.id];
        if (!currentRecord || currentRecord.runId !== record.runId) return current;
        return { ...current, [project.id]: { ...currentRecord, isCancelling: false } };
      });
      setPromptNotice(localMessage(error, "停止任务失败，请稍后重试。"));
    }
  }

  async function handlePromptSubmit(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		const task = taskPrompt.trim();
		if (!task) return;
		if (!currentProject) {
			setPromptNotice("请先创建项目，再提交任务。");
			openCreateProject();
			return;
		}
		await submitWorkspaceTask(task, currentProject);
	}

  function openHome() {
    setView("home");
    setTaskPrompt("");
    setPromptNotice(null);
  }

  function startNewProjectTask(projectId: string) {
    const project = projects.find((item) => item.id === projectId);
    if (!project) return;

    workspaceSubscriptions.current.get(project.id)?.();
    workspaceSubscriptions.current.delete(project.id);
    delete workspaceRunIds.current[project.id];
    delete workspaceRunReferences.current[project.id];
    saveWorkspaceRunReferences(workspaceRunReferences.current);
    restoredWorkspaceProjects.current.add(project.id);
    setWorkspaceRuns((current) => {
      const next = { ...current };
      delete next[project.id];
      return next;
    });
    selectProject(project);
    setTaskPrompt("");
    setPromptNotice(null);
  }

  function openProjectHistory(project: Project) {
    setHistoryProject(project);
  }

  function openCreateProject() {
    setProjectForm({ name: "", websiteUrl: "" });
    setProjectErrors({});
    setIsProjectDialogOpen(true);
  }

  function closeCreateProject() {
    setIsProjectDialogOpen(false);
    setProjectErrors({});
  }

  function openDeleteProject(project: Project) {
    setProjectToDelete(project);
    setProjectDeleteError(null);
  }

  function closeDeleteProject() {
    if (isDeletingProject) return;
    setProjectToDelete(null);
    setProjectDeleteError(null);
  }

  function removeProjectLocally(project: Project) {
    const nextProjects = projects.filter((item) => item.id !== project.id);
    setProjects(nextProjects);
    saveProjects(nextProjects);
    workspaceSubscriptions.current.get(project.id)?.();
    workspaceSubscriptions.current.delete(project.id);
    delete workspaceRunIds.current[project.id];
    delete workspaceRunReferences.current[project.id];
    saveWorkspaceRunReferences(workspaceRunReferences.current);
    setWorkspaceRuns((current) => {
      const next = { ...current };
      delete next[project.id];
      return next;
    });
    restoredWorkspaceProjects.current.delete(project.id);
    setCurrentProject((current) => {
      if (current?.id !== project.id) return current;
      return nextProjects[0] ?? null;
    });
  }

  async function confirmDeleteProject() {
    const project = projectToDelete;
    if (!project || isDeletingProject) return;
    setIsDeletingProject(true);
    setProjectDeleteError(null);
    try {
      await projectRegistrations.current.get(project.id);
      const activeDescriptor = await bridge.getDescriptor();
      setDescriptor(activeDescriptor);
      setBackend({ state: "READY" });
      showBackendReadyNoticeOnce();
      const client = createClient(activeDescriptor);
      if (!client.deleteProject) {
        throw new Error("delete_project_unavailable");
      }
      try {
        await client.deleteProject(project.id);
      } catch (error) {
        // A project created while the sidecar was unavailable has no server row
        // yet; local removal is still safe and makes delete idempotent in the UI.
        if (!(error instanceof LocalControlPlaneProblem && error.errorCode === "project_not_found")) throw error;
      }
      removeProjectLocally(project);
      setProjectToDelete(null);
    } catch (error) {
      setProjectDeleteError(localMessage(error, "删除项目失败，请稍后重试。"));
    } finally {
      setIsDeletingProject(false);
    }
  }

	function openAccountProfile() {
		isAccountProfileFormDirty.current = false;
		setAccountProfileForm(accountProfile);
		setAccountProfileError(null);
		setIsAccountDialogOpen(true);
	}

	function closeAccountProfile() {
		if (isSavingAccountProfile) return;
		isAccountProfileFormDirty.current = false;
		setIsAccountDialogOpen(false);
		setAccountProfileError(null);
	}

	async function saveAccountProfile(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		let activeDescriptor: BackendDescriptor;
		try {
			activeDescriptor = await bridge.getDescriptor();
			setDescriptor(activeDescriptor);
			setBackend({ state: "READY" });
      showBackendReadyNoticeOnce();
		} catch {
			setAccountProfileError("本地后端尚未就绪，请稍后重试。");
			return;
		}
		const client = createClient(activeDescriptor);
		if (!client.updateAccountProfile) {
			setAccountProfileError("账户资料服务暂不可用，请稍后重试。");
			return;
		}
		setIsSavingAccountProfile(true);
		setAccountProfileError(null);
		try {
			const saved = await client.updateAccountProfile({
				...accountProfileForm,
				name: accountProfileForm.name.trim(),
				email: accountProfileForm.email.trim(),
				work: accountProfileForm.work.trim(),
				organization: accountProfileForm.organization.trim()
			});
			setAccountProfile(saved);
			setAccountProfileForm(saved);
			isAccountProfileFormDirty.current = false;
			setIsAccountDialogOpen(false);
		} catch (error) {
			setAccountProfileError(localMessage(error, "保存失败，请检查邮箱和年龄后重试。"));
		} finally {
			setIsSavingAccountProfile(false);
		}
	}

	function changeAccountProfileForm(update: (current: AccountProfile) => AccountProfile) {
		isAccountProfileFormDirty.current = true;
		setAccountProfileForm(update);
	}

  function openModelServiceDialog() {
    setModelServiceForm(defaultModelServiceForm);
    setModelServiceTestResult(null);
		setModelServiceError(null);
    setIsModelServiceDialogOpen(true);
		if (!descriptor) return;
		const client = createClient(descriptor);
		if (!client.listModelServices) return;
		void client.listModelServices().then(setModelServices, (error) => {
			setModelServiceError(localMessage(error, "无法读取模型服务列表，请稍后重试。"));
		});
  }

  function closeModelServiceDialog() {
		if (isSavingModelService || isTestingModelService) return;
    setIsModelServiceDialogOpen(false);
    setModelServiceTestResult(null);
		setModelServiceError(null);
  }

  function modelServiceRequest(): ModelServiceRequest {
		return {
			name: modelServiceForm.name.trim(),
			api_base: modelServiceForm.baseUrl.trim(),
			api_key: modelServiceForm.apiKey.trim(),
			model: modelServiceForm.model.trim(),
			response_mode: modelServiceForm.responseMode
		};
	}

  function hasCompleteModelServiceForm(): boolean {
		return Boolean(modelServiceForm.name.trim() && modelServiceForm.baseUrl.trim() && modelServiceForm.apiKey.trim() && modelServiceForm.model.trim());
	}

	async function activeModelServiceClient(): Promise<ControlPlaneApi | null> {
		try {
			const activeDescriptor = await bridge.getDescriptor();
			setDescriptor(activeDescriptor);
			setBackend({ state: "READY" });
      showBackendReadyNoticeOnce();
			return createClient(activeDescriptor);
		} catch {
			setModelServiceError("本地后端尚未就绪，请稍后重试。");
			return null;
		}
	}

  async function addModelService(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
		if (!hasCompleteModelServiceForm()) return;
		const client = await activeModelServiceClient();
		if (!client) return;
		if (!client.addModelService) {
			setModelServiceError("模型服务配置接口暂不可用，请稍后重试。");
			return;
		}
		setIsSavingModelService(true);
		setModelServiceError(null);
		setModelServiceTestResult(null);
		try {
			const service = await client.addModelService(modelServiceRequest());
			setModelServices((current) => [service, ...current.filter((item) => item.name !== service.name)]);
			setModelServiceForm(defaultModelServiceForm);
		} catch (error) {
			setModelServiceError(localMessage(error, "模型服务保存失败，请检查配置后重试。"));
		} finally {
			setIsSavingModelService(false);
		}
  }

  async function testCurrentModelService() {
		if (!hasCompleteModelServiceForm()) return;
		const client = await activeModelServiceClient();
		if (!client) return;
		if (!client.testModelService) {
			setModelServiceError("模型服务连接测试接口暂不可用，请稍后重试。");
			return;
		}
		setIsTestingModelService(true);
		setModelServiceError(null);
		setModelServiceTestResult(null);
		try {
			showModelServiceTestResult(await client.testModelService(modelServiceRequest()));
		} catch (error) {
			setModelServiceError(localMessage(error, "连接测试失败，请稍后重试。"));
		} finally {
			setIsTestingModelService(false);
		}
  }

  async function testModelService(service: ModelServiceSummary) {
		const client = await activeModelServiceClient();
		if (!client) return;
		if (!client.testSavedModelService) {
			setModelServiceError("模型服务连接测试接口暂不可用，请稍后重试。");
			return;
		}
		setIsTestingModelService(true);
		setModelServiceError(null);
		setModelServiceTestResult(null);
		try {
			showModelServiceTestResult(await client.testSavedModelService(service.name));
		} catch (error) {
			setModelServiceError(localMessage(error, "连接测试失败，请稍后重试。"));
		} finally {
			setIsTestingModelService(false);
		}
  }

	function showModelServiceTestResult(result: ModelServiceTestResult) {
		if (result.success) {
			setModelServiceTestResult(`${result.name} 连接测试通过`);
			return;
		}
		setModelServiceError(modelServiceConnectionMessage(result.error_code));
	}

  function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors = validateProjectInput(projectForm);
    setProjectErrors(errors);
    if (Object.keys(errors).length > 0) return;

    const project = createProject(projectForm);
    const nextProjects = [project, ...projects];
    setProjects(nextProjects);
    saveProjects(nextProjects);
    setCurrentProject(project);
    closeCreateProject();
		if (descriptor) {
			const client = createClient(descriptor);
			if (client.registerProject) {
				const registration = client.registerProject(project.id, { website_url: project.websiteUrl })
					.then(() => undefined)
					.catch(() => undefined);
				projectRegistrations.current.set(project.id, registration);
				void registration.finally(() => {
					if (projectRegistrations.current.get(project.id) === registration) {
						projectRegistrations.current.delete(project.id);
					}
				});
			}
		}
		const task = taskPrompt.trim();
		if (task) void submitWorkspaceTask(task, project);
  }

  function selectProject(project: Project) {
    setCurrentProject(project);
    setProjects((current) => {
      const nextProjects = [project, ...current.filter((item) => item.id !== project.id)];
      saveProjects(nextProjects);
      return nextProjects;
    });
    setView("home");
    setPromptNotice(null);
  }

  function renderBatchOperations() {
    const snapshotError = readableRunError(projection?.snapshot.error);
    return (
      <div className="batch-page">
        <div className="page-heading page-heading--compact">
          <button className="back-link" type="button" onClick={() => setView("settings")}>
            <Icon name="arrow-left" />
            返回设置
          </button>
          <div>
            <p className="eyebrow">设置 / 批量操作</p>
            <h1>批量操作</h1>
            <p>配置任务文件并批量运行 WebRetriever 任务。</p>
            {currentProject && (
              <div className="batch-project-context" aria-label={`运行项目：${currentProject.name}`}>
                <span>当前项目</span>
                <strong>{currentProject.name}</strong>
                <span>任务起始 URL：{currentProject.websiteUrl}</span>
              </div>
            )}
          </div>
        </div>

        {backend.state === "FAILED" && (
          <section className="notice notice--error" role="alert">
            <div>
              <strong>本地执行后端暂时不可用</strong>
              <p>{backend.message ?? "无法连接本地执行后端。"}</p>
            </div>
            <button className="button button--secondary" type="button" onClick={() => void bridge.retrySidecar()}>重试后端</button>
          </section>
        )}

        <section className="run-card" aria-label="运行配置">
          <div className="section-heading">
            <div>
              <p className="eyebrow">任务执行</p>
              <h2>运行配置</h2>
            </div>
            {backend.state !== "READY" && (
              <span className={`connection-state connection-state--${backend.state.toLowerCase()}`}>
                <span aria-hidden="true" />
                {backendLabels[backend.state]}
              </span>
            )}
          </div>
          <div className="run-fields">
            <label>
              <span>任务文件</span>
              <div className="field-with-action">
                <input aria-label="任务文件" type="text" value={inputPath} onChange={(event) => changeInput(event.target.value, setInputPath)} disabled={!ready} placeholder="选择 JSON 任务文件" />
                <button className="button button--secondary" type="button" disabled={!ready} onClick={() => void pickTaskFile()}>选择文件</button>
              </div>
            </label>
            <label>
              <span>输出目录</span>
              <div className="field-with-action">
                <input aria-label="输出目录" type="text" value={outputRoot} onChange={(event) => changeInput(event.target.value, setOutputRoot)} disabled={!ready} placeholder="选择结果保存目录" />
                <button className="button button--secondary" type="button" disabled={!ready} onClick={() => void pickOutputDirectory()}>选择目录</button>
              </div>
            </label>
            <label>
              <span>模型配置</span>
              <input aria-label="模型配置" type="text" value={profileId} onChange={(event) => changeInput(event.target.value, setProfileId)} disabled={!ready} placeholder="输入模型配置名称" />
            </label>
          </div>
          <div className="run-actions">
            <button className="button button--secondary" type="button" disabled={!ready} onClick={() => void preflightRun()}>预检任务</button>
            <button className="button button--primary" type="button" disabled={!ready || !preflight} onClick={() => void startRun()}>开始运行</button>
          </div>
          {preflight && <p className="inline-status" role="status"><Icon name="check" />预检通过：{preflight.task_count} 个任务</p>}
          {preflightError && <p className="inline-status inline-status--error" role="alert">{preflightError}</p>}
          {activeRun && <p className="inline-status" role="status">运行 {activeRun.run_id} 已开始</p>}
          {runSnapshot && <p className="inline-status" role="status">运行状态：{projection?.snapshot.status ?? runSnapshot.status}</p>}
          {runError && <p className="inline-status inline-status--error" role="alert">{runError}</p>}
        </section>

        {projection && (
          <section className="run-card run-details" aria-label="运行详情">
            <div className="section-heading">
              <div>
                <p className="eyebrow">实时反馈</p>
                <h2>运行详情</h2>
              </div>
              <span className="detail-badge">RUN</span>
            </div>
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
            {projection.errors.length === 0 && !snapshotError ? <p>暂无错误</p> : (
              <ul>
                {snapshotError && !projection.errors.some((error) => error.message === snapshotError || error.code === snapshotError) && (
                  <li key="run-error">{snapshotError}</li>
                )}
                {projection.errors.map((error) => (
                  <li key={error.eventId}>{error.message ?? error.code}</li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark">
          <span className="brand-name">webAgent</span>
          <span className="brand-subtitle">智能工作台</span>
        </div>
        <button className="create-project" type="button" onClick={openCreateProject}>
          <Icon name="plus" />
          创建项目
        </button>
        <div className="sidebar-section">
          <p className="sidebar-label">最近项目</p>
          <button className={`workspace-link ${view === "home" ? "workspace-link--active" : ""}`} type="button" onClick={openHome}>
            <Icon name="grid" />
            我的工作区
          </button>
          {projects.map((project) => (
            <div className="project-entry" key={project.id}>
              <button
                className={`project-link ${currentProject?.id === project.id ? "project-link--active" : ""}`}
                type="button"
                onClick={() => selectProject(project)}
                aria-label={`项目 ${project.name}`}
              >
                <span className="project-link-mark" aria-hidden="true">{project.name.slice(0, 1)}</span>
                <span className="project-link-copy">
                  <strong>{project.name}</strong>
                  <small>{project.websiteUrl}</small>
                </span>
              </button>
              <div className="project-entry-actions">
                <ProjectNewTaskButton projectId={project.id} onCreate={startNewProjectTask} />
                <button
                  className="project-history-button"
                  type="button"
                  onClick={() => openProjectHistory(project)}
                  aria-label={`查看项目 ${project.name} 的历史任务`}
                >
                  历史
                </button>
                <button
                  className="project-delete-button"
                  type="button"
                  onClick={() => openDeleteProject(project)}
                  aria-label={`删除项目 ${project.name}`}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
        <button className="sidebar-profile" type="button" onClick={openAccountProfile} aria-label="打开账户设置">
          <span className="profile-avatar">{accountProfile.name.slice(0, 1) || "我"}</span>
          <span>
            <strong>{accountProfile.name || "未设置姓名"}</strong>
            <small>个人空间</small>
          </span>
        </button>
      </aside>

      <main className="app-main">
        <header className="topbar">
          <div className="mobile-brand">webAgent</div>
          <nav className="top-navigation" aria-label="主导航">
            <button className={`top-link ${view === "settings" || view === "batch" ? "top-link--active" : ""}`} type="button" aria-current={view === "settings" || view === "batch" ? "page" : undefined} onClick={() => setView("settings")}>设置</button>
            <button className={`top-link ${view === "help" ? "top-link--active" : ""}`} type="button" aria-current={view === "help" ? "page" : undefined} onClick={() => setView("help")}>帮助</button>
          </nav>
        </header>

        <div className="content-area">
          {isBackendReadyNoticeVisible && <span className="backend-status backend-status--ready" aria-live="polite" role="status">
            <span aria-hidden="true" />
            后端已就绪
          </span>}
          {view === "home" && (
            <section className="home-page" aria-label="我的工作区">
              <div className="home-intro">
                <h1>你好，{accountProfile.name || "朋友"}</h1>
                <p>今天想一起完成什么？</p>
                {currentProject && (
                  <div className="current-project" aria-label={`当前项目：${currentProject.name}`}>
                    <span>当前项目</span>
                    <strong>{currentProject.name}</strong>
                    <span className="current-project-url">{currentProject.websiteUrl}</span>
                  </div>
                )}
              </div>
              {currentWorkspaceRun ? (
                <section className="task-conversation" aria-label="任务执行过程" aria-live="polite">
                  <article className="conversation-message conversation-message--user">
                    <p>用户指令</p>
                    <strong>{currentWorkspaceRun.instruction}</strong>
                  </article>
                  {currentWorkspaceRun.projection.steps
                    .filter((step) => step.taskId in currentWorkspaceRun.projection.tasks)
                    .map((step) => (
                      <article className="conversation-message conversation-message--thinking" key={step.eventId}>
                        <p>模型思考过程</p>
                        <strong>第 {step.step} / {step.maxSteps} 步</strong>
                        {step.thought && <span>{step.thought}</span>}
                        <span>
                          {step.status === "PENDING"
                            ? `正在执行 ${step.action}…`
                            : `已执行 ${step.action}，结果：${step.outcome}`}
                        </span>
                        <div
                          aria-label={`任务进度：第 ${step.step} / ${step.maxSteps} 步`}
                          aria-valuemax={step.maxSteps}
                          aria-valuemin={0}
                          aria-valuenow={step.step}
                          className="task-progress-bar"
                          role="progressbar"
                        >
                          <span style={{ width: `${Math.min(100, (step.step / step.maxSteps) * 100)}%` }} />
                        </div>
                      </article>
                    ))}
                  {Object.values(currentWorkspaceRun.projection.tasks)
                    .filter((task) => task.answer !== null)
                    .map((task) => (
                      <article className="conversation-message conversation-message--answer" key={`${task.taskId}-answer`}>
                        <p>模型回答</p>
                        <strong>{task.answer}</strong>
                      </article>
                    ))}
                  {currentWorkspaceRunError && (
                    <article className="conversation-message conversation-message--error" role="alert">
                      <p>运行错误</p>
                      <strong>{currentWorkspaceRunError}</strong>
                    </article>
                  )}
                  <div className="conversation-actions">
                    <span>{workspaceRunStatus(currentWorkspaceRun.projection, null, currentWorkspaceRun.accepted)}</span>
                    {canCancelWorkspaceRun(currentWorkspaceRun.projection, null, currentWorkspaceRun.accepted) && (
                      <button
                        className="button button--danger"
                        disabled={currentWorkspaceRun.isCancelling}
                        onClick={() => void cancelWorkspaceTask()}
                        type="button"
                      >
                        {currentWorkspaceRun.isCancelling ? "正在停止…" : "停止任务"}
                      </button>
                    )}
                  </div>
                </section>
               ) : currentProject ? (
                 <section className="task-conversation task-conversation--empty" aria-label="任务执行过程">
                   <p>暂无任务记录</p>
                   <span>提交任务后，最近一次交互记录会显示在这里。</span>
                 </section>
               ) : null}
               <form className="task-composer" onSubmit={handlePromptSubmit}>
                <input
                  aria-label="任务描述"
                  type="text"
                  value={taskPrompt}
                  onChange={(event) => { setTaskPrompt(event.target.value); setPromptNotice(null); }}
                  placeholder="描述你的任务，例如：帮我整理一份竞品分析"
                />
                <button className="composer-submit" type="submit" aria-label="提交任务" disabled={!taskPrompt.trim()}>
                  <Icon name="arrow-up" />
                </button>
              </form>
              {promptNotice && <p className="composer-notice" role="status">{promptNotice}</p>}
            </section>
          )}

          {view === "settings" && (
            <section className="settings-page" aria-labelledby="settings-title">
              <div className="page-heading">
                <p className="eyebrow">工作台偏好</p>
                <h1 id="settings-title">设置</h1>
                <p>管理工作台偏好与任务执行入口。</p>
              </div>
              <div className="settings-group">
                <p className="settings-group-title">操作与管理</p>
                <button className="settings-card" type="button" aria-label="添加模型服务" onClick={openModelServiceDialog}>
                  <span className="settings-card-icon"><Icon name="layers" /></span>
                  <span className="settings-card-copy">
                    <strong>添加模型服务</strong>
                    <small>配置并管理模型服务连接</small>
                  </span>
                  <Icon name="arrow-right" />
                </button>
                <button className="settings-card" type="button" aria-label="批量操作" onClick={() => setView("batch")}>
                  <span className="settings-card-icon"><Icon name="layers" /></span>
                  <span className="settings-card-copy">
                    <strong>批量操作</strong>
                    <small>选择任务文件、输出目录与模型配置，批量执行任务</small>
                  </span>
                  <Icon name="arrow-right" />
                </button>
              </div>
            </section>
          )}

          {view === "help" && (
            <section className="help-page" aria-labelledby="help-title">
              <div className="page-heading">
                <p className="eyebrow">开始使用</p>
                <h1 id="help-title">帮助</h1>
                <p>从工作区开始描述你的目标，或在设置中打开批量操作运行任务。</p>
              </div>
              <div className="help-card">
                <span className="help-card-icon">?</span>
                <div>
                  <strong>如何开始</strong>
                  <p>在首页输入想要完成的事情；需要使用任务文件时，请进入“设置 → 批量操作”。</p>
                </div>
              </div>
            </section>
          )}

          {view === "batch" && renderBatchOperations()}
        </div>
      </main>
      {isProjectDialogOpen && (
        <div className="project-dialog-backdrop">
          <section className="project-dialog" role="dialog" aria-modal="true" aria-labelledby="create-project-title">
            <form onSubmit={handleCreateProject}>
              <h2 id="create-project-title">创建项目</h2>
              <label>
                <span>网站名称</span>
                <input
                  aria-label="网站名称"
                  aria-invalid={projectErrors.name ? "true" : undefined}
                  autoFocus
                  placeholder="例如：我的产品官网"
                  value={projectForm.name}
                  onChange={(event) => setProjectForm((current) => ({ ...current, name: event.target.value }))}
                />
                {projectErrors.name && <small className="field-error" role="alert">{projectErrors.name}</small>}
              </label>
              <label>
                <span>网站 URL</span>
                <input
                  aria-label="网站 URL"
                  aria-invalid={projectErrors.websiteUrl ? "true" : undefined}
                  placeholder="https://example.com"
                  value={projectForm.websiteUrl}
                  onChange={(event) => setProjectForm((current) => ({ ...current, websiteUrl: event.target.value }))}
                />
                {projectErrors.websiteUrl && <small className="field-error" role="alert">{projectErrors.websiteUrl}</small>}
              </label>
              <div className="project-dialog-actions">
                <button className="button button--secondary" type="button" onClick={closeCreateProject}>取消</button>
                <button className="button button--primary" type="submit">确定</button>
              </div>
            </form>
          </section>
        </div>
      )}
      {projectToDelete && (
        <div className="project-dialog-backdrop">
          <section className="project-dialog project-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title">
            <h2 id="delete-project-title">删除项目</h2>
            <p className="project-delete-dialog__description">
              确定删除“{projectToDelete.name}”吗？该项目的任务记录、交互事件和输出文件都会被删除。
            </p>
            {projectDeleteError && <p className="field-error" role="alert">{projectDeleteError}</p>}
            <div className="project-dialog-actions">
              <button className="button button--secondary" type="button" onClick={closeDeleteProject} disabled={isDeletingProject}>取消</button>
              <button className="button button--danger" type="button" onClick={() => void confirmDeleteProject()} disabled={isDeletingProject}>
                {isDeletingProject ? "删除中…" : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      )}
      {isAccountDialogOpen && (
        <div className="project-dialog-backdrop">
          <section className="project-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-profile-title">
            <form onSubmit={saveAccountProfile}>
              <h2 id="account-profile-title">账户设置</h2>
              <p className="account-dialog-description">填写你的真实资料，让 webAgent 更了解你。</p>
              <div className="account-dialog-fields">
                <label>
                  <span>姓名</span>
                  <input aria-label="姓名" autoFocus value={accountProfileForm.name} onChange={(event) => changeAccountProfileForm((current) => ({ ...current, name: event.target.value }))} />
                </label>
                <label>
                  <span>邮箱</span>
                  <input aria-label="邮箱" type="email" value={accountProfileForm.email} onChange={(event) => changeAccountProfileForm((current) => ({ ...current, email: event.target.value }))} />
                </label>
                <label>
                  <span>年龄</span>
                  <input aria-label="年龄" type="number" min="0" max="150" step="1" value={accountProfileForm.age ?? ""} onChange={(event) => changeAccountProfileForm((current) => ({ ...current, age: event.target.value === "" ? null : Number(event.target.value) }))} />
                </label>
                <label>
                  <span>工作</span>
                  <input aria-label="工作" value={accountProfileForm.work} onChange={(event) => changeAccountProfileForm((current) => ({ ...current, work: event.target.value }))} />
                </label>
                <label className="account-dialog-fields__full-width">
                  <span>组织</span>
                  <input aria-label="组织" value={accountProfileForm.organization} onChange={(event) => changeAccountProfileForm((current) => ({ ...current, organization: event.target.value }))} />
                </label>
              </div>
              {accountProfileError && <p className="field-error" role="alert">{accountProfileError}</p>}
              <div className="project-dialog-actions">
                <button className="button button--secondary" type="button" onClick={closeAccountProfile} disabled={isSavingAccountProfile}>取消</button>
                <button className="button button--primary" type="submit" disabled={isSavingAccountProfile}>{isSavingAccountProfile ? "保存中…" : "保存"}</button>
              </div>
            </form>
          </section>
        </div>
      )}
      {isModelServiceDialogOpen && (
        <div className="project-dialog-backdrop">
          <section className="project-dialog model-service-dialog" role="dialog" aria-modal="true" aria-labelledby="model-service-title">
            <form onSubmit={addModelService}>
              <h2 id="model-service-title">添加模型服务</h2>
              <div className="model-service-list">
                <div className="model-service-list__header">
                  <strong>已有模型服务</strong>
                  {modelServices.length === 0 ? (
                    <p>尚未添加模型服务</p>
                  ) : (
                    <ul>
                      {modelServices.map((service) => (
                        <li key={service.name}>
                          <div>
                            <strong>{service.name}</strong>
                            <small>{service.api_base}</small>
                            <span aria-label={`模型：${service.model}`}>{service.model}</span>
                            <span aria-label={`响应模式：${service.response_mode === "responses" ? "Responses" : "Chat Completions"}`}>{service.response_mode === "responses" ? "Responses" : "Chat Completions"}</span>
                          </div>
                          <button className="button button--secondary" type="button" aria-label={`测试 ${service.name} 连接`} disabled={isTestingModelService} onClick={() => void testModelService(service)}>测试连接</button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <button className="button button--secondary" type="button" aria-label="测试当前配置" disabled={!hasCompleteModelServiceForm() || isTestingModelService || isSavingModelService} onClick={() => void testCurrentModelService()}>{isTestingModelService ? "测试中…" : "测试连接"}</button>
              </div>
              {modelServiceTestResult && <p className="model-service-test-status" role="status">{modelServiceTestResult}</p>}
						{modelServiceError && <p className="field-error" role="alert">{modelServiceError}</p>}
              <div className="account-dialog-fields">
						<label className="account-dialog-fields__full-width">
							<span>模型服务名称</span>
							<input aria-label="模型服务名称" placeholder="例如：response" value={modelServiceForm.name} onChange={(event) => setModelServiceForm((current) => ({ ...current, name: event.target.value }))} />
						</label>
                <label className="account-dialog-fields__full-width">
                  <span>Base URL</span>
                  <input aria-label="Base URL" autoFocus placeholder="https://api.example.com/v1" value={modelServiceForm.baseUrl} onChange={(event) => setModelServiceForm((current) => ({ ...current, baseUrl: event.target.value }))} />
                </label>
                <label>
                  <span>API Key</span>
                  <input aria-label="API Key" type="password" placeholder="输入 API Key" value={modelServiceForm.apiKey} onChange={(event) => setModelServiceForm((current) => ({ ...current, apiKey: event.target.value }))} />
                </label>
                <label>
                  <span>模型</span>
                  <input aria-label="模型" placeholder="例如：gpt-4.1-mini" value={modelServiceForm.model} onChange={(event) => setModelServiceForm((current) => ({ ...current, model: event.target.value }))} />
                </label>
                <label className="account-dialog-fields__full-width">
                  <span>响应模式</span>
                  <select aria-label="响应模式" value={modelServiceForm.responseMode} onChange={(event) => setModelServiceForm((current) => ({ ...current, responseMode: event.target.value as ModelServiceForm["responseMode"] }))}>
                    <option value="chat-completions">Chat Completions</option>
                    <option value="responses">Responses</option>
                  </select>
                </label>
              </div>
              <div className="project-dialog-actions">
                <button className="button button--secondary" type="button" onClick={closeModelServiceDialog} disabled={isSavingModelService || isTestingModelService}>取消</button>
                <button className="button button--primary" type="submit" disabled={!hasCompleteModelServiceForm() || isSavingModelService || isTestingModelService}>{isSavingModelService ? "添加中…" : "添加服务"}</button>
              </div>
            </form>
          </section>
        </div>
      )}
      {historyProject && (
        <ProjectHistoryDialog
          projectId={historyProject.id}
          projectName={historyProject.name}
          open
          loadHistory={loadProjectHistory}
          onClose={() => setHistoryProject(null)}
        />
      )}
    </div>
  );
}

function workspaceRunStatus(
  projection: RunProjection | null,
  snapshot: RunSnapshot | null,
  accepted: RunAccepted | null
): string {
  return `运行状态：${projection?.snapshot.status ?? snapshot?.status ?? accepted?.status ?? "STARTING"}`;
}

function canCancelWorkspaceRun(
  projection: RunProjection | null,
  snapshot: RunSnapshot | null,
  accepted: RunAccepted | null
): boolean {
  const status = projection?.snapshot.status ?? snapshot?.status ?? accepted?.status;
  return status === "STARTING" || status === "RUNNING" || status === "CANCELLING";
}

function Icon({ name }: { name: "plus" | "grid" | "layers" | "arrow-right" | "arrow-left" | "arrow-up" | "check" }) {
  const paths = {
    plus: <><path d="M12 5v14M5 12h14" /></>,
    grid: <><rect x="4" y="4" width="6" height="6" rx="1" /><rect x="14" y="4" width="6" height="6" rx="1" /><rect x="4" y="14" width="6" height="6" rx="1" /><rect x="14" y="14" width="6" height="6" rx="1" /></>,
    layers: <><path d="m12 4 8 4-8 4-8-4 8-4Z" /><path d="m4 12 8 4 8-4M4 16l8 4 8-4" /></>,
    "arrow-right": <><path d="M5 12h14M13 6l6 6-6 6" /></>,
    "arrow-left": <><path d="M19 12H5M11 18l-6-6 6-6" /></>,
    "arrow-up": <><path d="M12 19V5M6 11l6-6 6 6" /></>,
    check: <><path d="m5 12 4 4L19 6" /></>
  };

  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function localMessage(error: unknown, fallback: string): string {
  if (error instanceof LocalControlPlaneProblem) return localizeProblem(error);
  return error instanceof TypeError ? `无法连接本地后端：${error.message}` : fallback;
}

function readableRunError(error: unknown): string | null {
  if (typeof error === "string" && error.trim()) return error;
  if (typeof error !== "object" || error === null || Array.isArray(error)) return null;
  const record = error as Record<string, unknown>;
  if (typeof record.message === "string" && record.message.trim()) return record.message;
  if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
  if (typeof record.error === "string" && record.error.trim()) return record.error;
  if (typeof record.code === "string" && record.code.trim()) return record.code;
  return null;
}

function modelServiceConnectionMessage(errorCode: string | null): string {
	const messages: Record<string, string> = {
		authentication_failed: "连接测试失败：API Key 无效或没有访问权限。",
		connection_timed_out: "连接测试超时，请检查服务地址和网络。",
		connection_failed: "连接测试失败，请检查服务地址和网络。",
		provider_unavailable: "模型服务暂不可用，请稍后重试。",
		request_rejected: "连接测试请求被模型服务拒绝，请检查模型和响应模式。"
	};
	return messages[errorCode ?? ""] ?? "连接测试失败，请检查配置后重试。";
}
