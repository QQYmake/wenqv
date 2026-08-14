import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "./components/Composer";
import { SettingsPanel } from "./components/SettingsPanel";
import type { AgentApi } from "./api/client";

const maskedView = {
  has_config: true,
  main: { base_url: "https://api.example.com/v1", api_key: "sk-***345", model: "gpt-test" },
  summary: { base_url: "", api_key: "", model: "" },
};

function makeClient(overrides?: Partial<AgentApi>) {
  return {
    getUserConfig: vi.fn().mockResolvedValue(maskedView),
    putUserConfig: vi.fn().mockResolvedValue(maskedView),
    testUserConfig: vi.fn().mockResolvedValue({ ok: true, detail: "pong" }),
    listModels: vi.fn().mockResolvedValue({ models: [] }),
    ...overrides,
  } as unknown as AgentApi;
}

describe("SettingsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders masked api key loaded from the server", async () => {
    render(<SettingsPanel client={makeClient()} onClose={vi.fn()} />);
    expect(await screen.findByText(/已保存：sk-\*\*\*345/)).toBeInTheDocument();
    // The plaintext is never rendered.
    expect(screen.queryByText(/sk-supersecret/)).not.toBeInTheDocument();
    const dialog = screen.getByRole("dialog", { name: "API 配置" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(await screen.findByRole("status")).toHaveTextContent("已配置");
  });

  it("announces when API configuration is still required", async () => {
    const client = makeClient({
      getUserConfig: vi.fn().mockResolvedValue({ ...maskedView, has_config: false }),
    });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const status = await screen.findByText("尚未配置");
    expect(status).toHaveAttribute("role", "status");
  });

  it("submits updated config and clears the api key field after save", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const keyField = await screen.findByPlaceholderText("sk-***345");
    await user.type(keyField, "sk-new-1234567890");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(client.putUserConfig).toHaveBeenCalledTimes(1));
    const body = vi.mocked(client.putUserConfig).mock.calls[0][0];
    expect(body.main.api_key).toBe("sk-new-1234567890");
    // After save the editable key field is cleared (masked stays server-side).
    await waitFor(() => expect(keyField).toHaveValue(""));
  });

  it("test connection shows success then failure states", async () => {
    const user = userEvent.setup();
    let ok = true;
    const client = makeClient({
      testUserConfig: vi.fn().mockImplementation(() =>
        ok
          ? Promise.resolve({ ok: true, detail: "pong" })
          : Promise.resolve({ ok: false, detail: "401" }),
      ),
    });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const testButton = await screen.findByRole("button", { name: "测试连接" });

    await user.click(testButton);
    await waitFor(() => expect(screen.getByText("连接成功")).toBeInTheDocument());

    ok = false;
    await user.click(testButton);
    await waitFor(() => expect(screen.getByText(/连接失败：401/)).toBeInTheDocument());
  });

  it("fetches Main models and keeps the current Model until a candidate is chosen", async () => {
    const user = userEvent.setup();
    const listModels = vi.fn().mockResolvedValue({ models: ["gpt-main-a", "gpt-main-b"] });
    const client = makeClient({ listModels });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);

    const main = screen.getByRole("group", { name: "主模型 (main)" });
    const modelInput = within(main).getByRole("combobox");
    await screen.findByDisplayValue("gpt-test");
    await user.click(within(main).getByRole("button", { name: "拉取" }));

    await waitFor(() =>
      expect(listModels).toHaveBeenCalledWith({
        role: "main",
        base_url: "https://api.example.com/v1",
        api_key: "",
      }),
    );
    expect(modelInput).toHaveValue("gpt-test");

    await user.click(modelInput);
    expect(await screen.findByRole("option", { name: "gpt-main-a" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "gpt-main-b" }));
    expect(modelInput).toHaveValue("gpt-main-b");
  });

  it("fetches Summary models independently", async () => {
    const user = userEvent.setup();
    const listModels = vi.fn().mockImplementation(({ role }: { role: string }) =>
      Promise.resolve({ models: role === "summary" ? ["summary-a"] : ["main-a"] }),
    );
    const client = makeClient({ listModels });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);

    const summary = screen.getByRole("group", { name: "摘要模型 (summary, 可选)" });
    await user.click(within(summary).getByRole("button", { name: "拉取" }));
    await waitFor(() => expect(listModels).toHaveBeenCalledWith({ role: "summary", base_url: "", api_key: "" }));
    const summaryInput = within(summary).getByRole("combobox");
    await user.click(summaryInput);
    expect(await screen.findByRole("option", { name: "summary-a" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "main-a" })).not.toBeInTheDocument();
  });

  it("keeps Model freely editable", async () => {
    const user = userEvent.setup();
    render(<SettingsPanel client={makeClient()} onClose={vi.fn()} />);
    const modelInput = within(screen.getByRole("group", { name: "主模型 (main)" })).getByRole("combobox");
    await screen.findByDisplayValue("gpt-test");
    await user.clear(modelInput);
    await user.type(modelInput, "provider-specific-model");
    expect(modelInput).toHaveValue("provider-specific-model");
  });

  it("shows the same non-selectable empty state for empty and failed discovery", async () => {
    const user = userEvent.setup();
    const listModels = vi.fn().mockRejectedValue(new Error("502 provider detail"));
    const client = makeClient({ listModels });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const main = screen.getByRole("group", { name: "主模型 (main)" });
    await user.click(within(main).getByRole("button", { name: "拉取" }));
    await waitFor(() => expect(listModels).toHaveBeenCalled());
    await user.click(within(main).getByRole("combobox"));

    const empty = await screen.findByRole("option", { name: "未拉取到模型" });
    expect(empty).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("502 provider detail")).not.toBeInTheDocument();
  });

  it("shows the empty state when discovery succeeds without model IDs", async () => {
    const user = userEvent.setup();
    const listModels = vi.fn().mockResolvedValue({ models: [] });
    const client = makeClient({ listModels });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const main = screen.getByRole("group", { name: "主模型 (main)" });
    await user.click(within(main).getByRole("button", { name: "拉取" }));
    await waitFor(() => expect(listModels).toHaveBeenCalled());
    await user.click(within(main).getByRole("combobox"));
    expect(await screen.findByRole("option", { name: "未拉取到模型" })).toBeInTheDocument();
  });

  it("clears old candidates when the provider changes but keeps the Model value", async () => {
    const user = userEvent.setup();
    const listModels = vi.fn().mockResolvedValue({ models: ["old-provider-model"] });
    const client = makeClient({ listModels });
    render(<SettingsPanel client={client} onClose={vi.fn()} />);
    const main = screen.getByRole("group", { name: "主模型 (main)" });
    const modelInput = within(main).getByRole("combobox");
    await user.click(within(main).getByRole("button", { name: "拉取" }));
    await waitFor(() => expect(listModels).toHaveBeenCalled());
    await user.click(modelInput);
    expect(await screen.findByRole("option", { name: "old-provider-model" })).toBeInTheDocument();

    const baseUrl = within(main).getByLabelText("Base URL");
    await user.clear(baseUrl);
    await user.type(baseUrl, "https://new-provider.example/v1");
    const apiKey = within(main).getByLabelText(/API Key/);
    await user.type(apiKey, "sk-new-provider-key");

    expect(modelInput).toHaveValue("gpt-test");
    expect(screen.queryByRole("option", { name: "old-provider-model" })).not.toBeInTheDocument();
    await user.click(modelInput);
    expect(await screen.findByRole("option", { name: "未拉取到模型" })).toBeInTheDocument();
  });
});

describe("Composer disabled state", () => {
  it("disables the textarea and send button when api is not configured", () => {
    render(
      <Composer
        value=""
        modelId=""
        skills={[]}
        selectedSkills={new Set()}
        streaming={false}
        apiConfigured={false}
        reasoningEffort="medium"
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbort={vi.fn()}
        onToggleSkill={vi.fn()}
        onReasoningEffortChange={vi.fn()}
      />,
    );
    const textarea = screen.getByRole("textbox", { name: "消息" });
    expect(textarea).toBeDisabled();
    expect(screen.getByPlaceholderText("请先配置 API")).toBeInTheDocument();
    // canSend is false -> the send button uses its "请输入" label and is disabled.
    expect(screen.getByRole("button", { name: /请输入消息后发送/ })).toBeDisabled();
  });

  it("allows typing and sending when api is configured", () => {
    render(
      <Composer
        value="hi"
        modelId="m"
        skills={[]}
        selectedSkills={new Set()}
        streaming={false}
        apiConfigured
        reasoningEffort="medium"
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbort={vi.fn()}
        onToggleSkill={vi.fn()}
        onReasoningEffortChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("textbox", { name: "消息" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();
  });
});
