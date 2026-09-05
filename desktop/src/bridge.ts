import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

export type BackendState = "STARTING" | "READY" | "RESTARTING" | "DRAINING" | "STOPPED" | "FAILED";

export interface BackendDescriptor {
  baseUrl: string;
  bearerToken: string;
  protocolVersion: number;
}

export interface BackendStatus {
  state: BackendState;
  message?: string;
}

export interface DesktopBridge {
  getDescriptor(): Promise<BackendDescriptor>;
  retrySidecar(): Promise<BackendDescriptor>;
  onBackendStateChanged(listener: (status: BackendStatus) => void): Promise<UnlistenFn>;
  pickTaskFile(): Promise<string | null>;
  pickOutputDirectory(): Promise<string | null>;
}

export const tauriBridge: DesktopBridge = {
  getDescriptor: () => invoke<BackendDescriptor>("sidecar_descriptor"),
  retrySidecar: () => invoke<BackendDescriptor>("retry_sidecar"),
  onBackendStateChanged: async (listener) =>
    listen<BackendStatus>("backend-state-changed", (event) => listener(event.payload)),
  pickTaskFile: async () => {
    const selected = await open({ multiple: false, directory: false, filters: [{ name: "JSON", extensions: ["json"] }] });
    return typeof selected === "string" ? selected : null;
  },
  pickOutputDirectory: async () => {
    const selected = await open({ multiple: false, directory: true });
    return typeof selected === "string" ? selected : null;
  }
};
