import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { LocalControlPlaneProblem } from "../api/controlPlaneClient";

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

describe("账户设置", () => {
  beforeEach(() => localStorage.clear());

  it("从左下角头像编辑账户资料，并在重新打开后回显已保存信息", async () => {
    const user = userEvent.setup();
    let profile = {
      name: "林晓宇",
      email: "xiaoyu@example.com",
      age: 29,
      work: "产品设计师",
      organization: "webAgent"
    };
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      getAccountProfile: vi.fn().mockImplementation(() => Promise.resolve(profile)),
      updateAccountProfile: vi.fn().mockImplementation((nextProfile) => {
        profile = nextProfile;
        return Promise.resolve(profile);
      })
    };
    const bridge = readyBridge();
    const firstRender = render(<App bridge={bridge} createClient={() => client} />);

		await waitFor(() => expect(client.getAccountProfile).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    expect(await screen.findByRole("dialog", { name: "账户设置" })).toBeVisible();
    expect(screen.getByLabelText("邮箱")).toHaveValue("xiaoyu@example.com");

    await user.clear(screen.getByLabelText("姓名"));
    await user.type(screen.getByLabelText("姓名"), "王晨");
    await user.clear(screen.getByLabelText("工作"));
    await user.type(screen.getByLabelText("工作"), "研究员");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(client.updateAccountProfile).toHaveBeenCalledWith({
      name: "王晨",
      email: "xiaoyu@example.com",
      age: 29,
      work: "研究员",
      organization: "webAgent"
    }));
    expect(bridge.getDescriptor).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog", { name: "账户设置" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "你好，王晨" })).toBeVisible();

    firstRender.unmount();
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    expect(await screen.findByLabelText("姓名")).toHaveValue("王晨");
    expect(screen.getByLabelText("工作")).toHaveValue("研究员");
  });

  it("用户补全邮箱和年龄后仍能保存，即使初始资料随后才加载完成", async () => {
    const user = userEvent.setup();
    let resolveProfile: (profile: {
      name: string;
      email: string;
      age: number | null;
      work: string;
      organization: string;
    }) => void = () => {};
    const profileRequest = new Promise<{
      name: string;
      email: string;
      age: number | null;
      work: string;
      organization: string;
    }>((resolve) => {
      resolveProfile = resolve;
    });
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      getAccountProfile: vi.fn().mockImplementation(() => profileRequest),
      updateAccountProfile: vi.fn().mockImplementation((profile) => {
        if (!profile.email || profile.age === null) return Promise.reject(new Error("validation failed"));
        return Promise.resolve(profile);
      })
    };
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await waitFor(() => expect(client.getAccountProfile).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败，请检查邮箱和年龄后重试。");

    await user.type(screen.getByLabelText("邮箱"), "xiaoyu@example.com");
    await user.type(screen.getByLabelText("年龄"), "29");
    await act(async () => {
      resolveProfile({ name: "林晓宇", email: "", age: null, work: "", organization: "" });
      await Promise.resolve();
    });

    expect(screen.getByLabelText("邮箱")).toHaveValue("xiaoyu@example.com");
    expect(screen.getByLabelText("年龄")).toHaveValue(29);
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(client.updateAccountProfile).toHaveBeenLastCalledWith({
      name: "林晓宇",
      email: "xiaoyu@example.com",
      age: 29,
      work: "",
      organization: ""
    }));
    expect(screen.queryByRole("dialog", { name: "账户设置" })).not.toBeInTheDocument();
  });

  it("保存时会重新获取暂未加载的后端描述符并提交资料", async () => {
    const user = userEvent.setup();
    const descriptor = {
      baseUrl: "http://127.0.0.1:43127",
      bearerToken: "refreshed-token",
      protocolVersion: 1
    };
    const bridge = {
      ...readyBridge(),
      getDescriptor: vi.fn()
        .mockRejectedValueOnce(new Error("descriptor is not ready"))
        .mockResolvedValue(descriptor)
    };
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      updateAccountProfile: vi.fn().mockImplementation((profile) => Promise.resolve(profile))
    };
    render(<App bridge={bridge} createClient={() => client} />);

    await waitFor(() => expect(bridge.getDescriptor).toHaveBeenCalledTimes(1));
    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    await user.type(screen.getByLabelText("邮箱"), "xiaoyu@example.com");
    await user.type(screen.getByLabelText("年龄"), "18");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(bridge.getDescriptor).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(client.updateAccountProfile).toHaveBeenCalledWith({
      name: "林晓宇",
      email: "xiaoyu@example.com",
      age: 18,
      work: "",
      organization: ""
    }));
    expect(screen.queryByRole("dialog", { name: "账户设置" })).not.toBeInTheDocument();
  });

  it("保存失败时显示本地后端的实际错误，而不是误报资料校验失败", async () => {
    const user = userEvent.setup();
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      updateAccountProfile: vi.fn().mockRejectedValue(new LocalControlPlaneProblem(401, "unauthorized"))
    };
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("本地后端认证已失效，正在重新获取连接。");
  });

  it("保存失败时显示未知后端错误的 HTTP 状态", async () => {
    const user = userEvent.setup();
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      updateAccountProfile: vi.fn().mockRejectedValue(new LocalControlPlaneProblem(500, "unknown"))
    };
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("本地后端请求失败（HTTP 500），请稍后重试。");
  });

  it("保存请求未进入网络层时显示底层错误", async () => {
    const user = userEvent.setup();
    const client = {
      preflight: vi.fn(),
      createRun: vi.fn(),
      getRun: vi.fn(),
      subscribeToRun: vi.fn(),
      updateAccountProfile: vi.fn().mockRejectedValue(new TypeError("Failed to fetch"))
    };
    render(<App bridge={readyBridge()} createClient={() => client} />);

    await user.click(await screen.findByRole("button", { name: "打开账户设置" }));
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接本地后端：Failed to fetch");
  });
});
