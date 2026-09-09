import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

type ModelService = {
  name: string;
  api_base: string;
  model: string;
  response_mode: "responses" | "chat-completions";
};

function modelServiceClient(initialServices: ModelService[] = []) {
  let services = initialServices;
  return {
    preflight: vi.fn(),
    createRun: vi.fn(),
    getRun: vi.fn(),
    subscribeToRun: vi.fn(),
    listModelServices: vi.fn().mockImplementation(() => Promise.resolve(services)),
    addModelService: vi.fn().mockImplementation((input) => {
      const { api_key: _apiKey, ...service } = input;
      services = [service, ...services.filter((item) => item.name !== service.name)];
      return Promise.resolve(service);
    }),
    testModelService: vi.fn().mockImplementation((input) => Promise.resolve({ name: input.name, success: true, error_code: null })),
    testSavedModelService: vi.fn().mockImplementation((name) => Promise.resolve({ name, success: true, error_code: null }))
  };
}

describe("模型服务设置", () => {
  beforeEach(() => localStorage.clear());

  it("打开对话框时显示服务名称及指定默认值", async () => {
    const user = userEvent.setup();
    const client = modelServiceClient();
    render(<App bridge={readyBridge()} createClient={() => client} />);
		await screen.findByText("后端已就绪");

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(await screen.findByRole("button", { name: "添加模型服务" }));

    expect(await screen.findByRole("dialog", { name: "添加模型服务" })).toBeVisible();
    expect(screen.getByLabelText("模型服务名称")).toHaveValue("response");
    expect(screen.getByLabelText("模型")).toHaveValue("gpt-5.5");
    expect(screen.getByLabelText("响应模式")).toHaveValue("responses");
  });

  it("将填写的服务保存到后端配置并显示其名称", async () => {
    const user = userEvent.setup();
    const client = modelServiceClient();
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(await screen.findByRole("button", { name: "添加模型服务" }));
    await user.clear(screen.getByLabelText("模型服务名称"));
    await user.type(screen.getByLabelText("模型服务名称"), "primary");
    await user.type(screen.getByLabelText("Base URL"), "https://api.example.com/v1");
    await user.type(screen.getByLabelText("API Key"), "test-key");
    await user.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(client.addModelService).toHaveBeenCalledWith({
      name: "primary",
      api_base: "https://api.example.com/v1",
      api_key: "test-key",
      model: "gpt-5.5",
      response_mode: "responses"
    }));
    expect(await screen.findByText("primary")).toBeVisible();
    expect(screen.getByText("https://api.example.com/v1")).toBeVisible();
  });

  it("为未保存的当前配置调用真实连接测试接口", async () => {
    const user = userEvent.setup();
    const client = modelServiceClient();
    render(<App bridge={readyBridge()} createClient={() => client} />);
		await screen.findByText("后端已就绪");

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(await screen.findByRole("button", { name: "添加模型服务" }));
    await user.type(screen.getByLabelText("Base URL"), "https://api.example.com/v1");
    await user.type(screen.getByLabelText("API Key"), "test-key");
		await waitFor(() => expect(screen.getByRole("button", { name: "测试当前配置" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "测试当前配置" }));

    await waitFor(() => expect(client.testModelService).toHaveBeenCalledWith(expect.objectContaining({
      name: "response",
      model: "gpt-5.5",
      response_mode: "responses"
    })));
    expect(await screen.findByText("response 连接测试通过")).toBeVisible();
  });

  it("测试已保存的服务时仅按名称调用后端", async () => {
    const user = userEvent.setup();
    const client = modelServiceClient([{
      name: "primary",
      api_base: "https://api.example.com/v1",
      model: "gpt-5.4",
      response_mode: "responses"
    }]);
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(await screen.findByRole("button", { name: "添加模型服务" }));
    await user.click(await screen.findByRole("button", { name: "测试 primary 连接" }));

    await waitFor(() => expect(client.testSavedModelService).toHaveBeenCalledWith("primary"));
    expect(await screen.findByText("primary 连接测试通过")).toBeVisible();
  });

  it("长服务列表可滚动，且添加服务表单仍可访问", async () => {
    const user = userEvent.setup();
    const client = modelServiceClient(Array.from({ length: 10 }, (_, index) => ({
      name: `service-${index + 1}`,
      api_base: `https://api-${index + 1}.example.com/v1`,
      model: "gpt-5.4",
      response_mode: "responses" as const
    })));
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(await screen.findByRole("button", { name: "添加模型服务" }));

    const dialog = await screen.findByRole("dialog", { name: "添加模型服务" });
    const serviceList = dialog.querySelector(".model-service-list ul");

    expect(serviceList).not.toBeNull();
    expect(styles).toMatch(/\.model-service-list ul \{[^}]*max-height: 340px;[^}]*overflow-y: auto;/);
    expect(styles).toMatch(/\.model-service-dialog \{[^}]*max-height: calc\(100dvh - 40px\);[^}]*overflow-y: auto;/);
    expect(screen.getByLabelText("模型服务名称")).toBeVisible();
  });
});
