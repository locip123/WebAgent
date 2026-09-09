export interface ModelService {
  id: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  responseMode: string;
  createdAt: string;
}

export interface ModelServiceInput {
  baseUrl: string;
  apiKey: string;
  model: string;
  responseMode: string;
}

const MODEL_SERVICES_STORAGE_KEY = "webAgent.modelServices";

export function createModelService(input: ModelServiceInput): ModelService {
  return {
    id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    baseUrl: input.baseUrl.trim(),
    apiKey: input.apiKey.trim(),
    model: input.model.trim(),
    responseMode: input.responseMode,
    createdAt: new Date().toISOString()
  };
}

export function loadModelServices(storage: Storage | null = getLocalStorage()): ModelService[] {
  if (!storage) return [];
  try {
    const value: unknown = JSON.parse(storage.getItem(MODEL_SERVICES_STORAGE_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter(isModelService) : [];
  } catch {
    return [];
  }
}

export function saveModelServices(services: ModelService[], storage: Storage | null = getLocalStorage()): void {
  try {
    storage?.setItem(MODEL_SERVICES_STORAGE_KEY, JSON.stringify(services));
  } catch {
    // The service list remains available for the current desktop session.
  }
}

function getLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isModelService(value: unknown): value is ModelService {
  if (typeof value !== "object" || value === null) return false;
  const service = value as Partial<ModelService>;
  return (
    typeof service.id === "string" &&
    typeof service.baseUrl === "string" &&
    typeof service.apiKey === "string" &&
    typeof service.model === "string" &&
    typeof service.responseMode === "string" &&
    typeof service.createdAt === "string"
  );
}
