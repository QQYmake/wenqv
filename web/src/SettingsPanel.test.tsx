import { render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.getByRole("dialog", { name: "API 配置" })).toBeInTheDocument();
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
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbort={vi.fn()}
        onToggleSkill={vi.fn()}
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
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbort={vi.fn()}
        onToggleSkill={vi.fn()}
      />,
    );
    expect(screen.getByRole("textbox", { name: "消息" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();
  });
});