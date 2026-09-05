import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const configPath = resolve(process.cwd(), "src-tauri/tauri.conf.json");
const capabilityPath = resolve(process.cwd(), "src-tauri/capabilities/default.json");
const bridgePath = resolve(process.cwd(), "src/bridge.ts");

describe("desktop security boundary", () => {
  it("limits production networking and IPC to the local desktop control plane", async () => {
    const config = JSON.parse(await readFile(configPath, "utf8")) as {
      app: { security: { csp: string; devCsp?: string } };
    };
    const capability = JSON.parse(await readFile(capabilityPath, "utf8")) as { permissions: string[] };
    const bridgeSource = await readFile(bridgePath, "utf8");

    expect(config.app.security.csp).toContain("http://127.0.0.1:*");
    expect(config.app.security.csp).not.toMatch(/localhost:1420|ws:\/\//);
    expect(config.app.security.devCsp).toContain("http://localhost:1420");
    expect(capability.permissions).toEqual(["core:default", "dialog:allow-open"]);
    expect(bridgeSource).not.toMatch(/localStorage|sessionStorage|indexedDB/);
  });
});
