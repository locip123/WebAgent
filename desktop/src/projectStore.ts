export interface Project {
  id: string;
  name: string;
  websiteUrl: string;
  createdAt: string;
}

export interface ProjectInput {
  name: string;
  websiteUrl: string;
}

export interface ProjectValidationErrors {
  name?: string;
  websiteUrl?: string;
}

const PROJECTS_STORAGE_KEY = "webAgent.projects";

export function validateProjectInput(input: ProjectInput): ProjectValidationErrors {
  const errors: ProjectValidationErrors = {};
  const name = input.name.trim();
  const websiteUrl = input.websiteUrl.trim();

  if (!name) errors.name = "请输入网站名称";
  if (!websiteUrl) {
    errors.websiteUrl = "请输入网站 URL";
  } else {
    try {
      const parsed = new URL(websiteUrl);
      if (
        /\s/.test(websiteUrl) ||
        !["http:", "https:"].includes(parsed.protocol) ||
        !parsed.hostname ||
        parsed.username ||
        parsed.password
      ) {
        errors.websiteUrl = "请输入有效的网站 URL（以 http:// 或 https:// 开头）";
      }
    } catch {
      errors.websiteUrl = "请输入有效的网站 URL（以 http:// 或 https:// 开头）";
    }
  }

  return errors;
}

export function createProject(input: ProjectInput): Project {
  const name = input.name.trim();
  const websiteUrl = input.websiteUrl.trim();
  return {
    id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    name,
    websiteUrl,
    createdAt: new Date().toISOString()
  };
}

export function loadProjects(storage: Storage | null = getLocalStorage()): Project[] {
  if (!storage) return [];
  try {
    const value: unknown = JSON.parse(storage.getItem(PROJECTS_STORAGE_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];
    return value.filter(isProject);
  } catch {
    return [];
  }
}

export function saveProjects(projects: Project[], storage: Storage | null = getLocalStorage()): void {
  try {
    storage?.setItem(PROJECTS_STORAGE_KEY, JSON.stringify(projects));
  } catch {
    // A desktop session can still work when browser storage is unavailable.
  }
}

function getLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isProject(value: unknown): value is Project {
  if (typeof value !== "object" || value === null) return false;
  const project = value as Partial<Project>;
  return (
    typeof project.id === "string" &&
    typeof project.name === "string" &&
    typeof project.websiteUrl === "string" &&
    typeof project.createdAt === "string"
  );
}
