import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App, hydrateHistory } from "./App";
import type { AgentApi } from "./api/client";
import { MessageList } from "./components/MessageList";
import type { AgentEvent, ChatMessage, Session } from "./types";

function makeClient(options?: {
  sessions?: Session[];
  messages?: ChatMessage[];
  events?: AgentEvent[];
}) {
  const sessions = options?.sessions ?? [];
  const events = options?.events ?? [{ type: "text_delta", delta: "你好。" }, { type: "done" }];
  const client = {
    listSessions: vi.fn().mockResolvedValue(sessions),
    createSession: vi.fn().mockResolvedValue({ id: "new-session", title: "新对话" }),
    renameSession: vi.fn(async (id: string, title: string) => ({ id, title })),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    getMessages: vi.fn().mockResolvedValue(options?.messages ?? []),
    listSkills: vi.fn().mockResolvedValue([
      { name: "research", description: "按证据整理材料" },
      { name: "planner", description: "拆分行动步骤" },
    ]),
    getConfig: vi.fn().mockResolvedValue({
      model_id: "lake-main-1",
      features: { skills: true, abort: true },
    }),
    abortChat: vi.fn().mockResolvedValue(undefined),
    streamChat: vi.fn(async function* () {
      for (const event of events) yield event;
    }),
  };
  return client as unknown as AgentApi;
}

describe("App", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("shows the quiet welcome state and lets a suggestion seed the composer", async () => {
    const user = userEvent.setup();
    render(<App client={makeClient()} renderWater={false} />);

    expect(screen.getByRole("heading", { name: "今天，我们从哪里开始？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /梳理思路/ }));

    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue(
      "帮我把一个还很模糊的想法梳理成目标、约束与下一步。",
    );
    expect(await screen.findByText("lake-main-1")).toBeInTheDocument();
  });

  it("persists theme choice and expands the rail from its non-icon area", async () => {
    const user = userEvent.setup();
    render(<App client={makeClient()} renderWater={false} />);

    await user.click(screen.getByRole("button", { name: "切换为深色模式" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(localStorage.getItem("blue-lake.theme")).toBe("dark");

    await user.click(screen.getByRole("complementary", { name: "对话侧栏" }));
    expect(screen.getByRole("button", { name: "新对话" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭侧栏" }));
    expect(screen.getByRole("button", { name: "展开侧栏" })).toBeInTheDocument();
  });

  it("restores the remembered session and renders persisted markdown", async () => {
    localStorage.setItem("blue-lake.active-session", "s-1");
    const client = makeClient({
      sessions: [{ id: "s-1", title: "昨日问题" }],
      messages: [
        { id: "m-1", role: "user", content: "请解释" },
        { id: "m-2", role: "assistant", content: "## 结论\n\n内容已经恢复。" },
      ],
    });

    render(<App client={client} renderWater={false} />);

    expect(await screen.findByRole("heading", { name: "结论" })).toBeInTheDocument();
    expect(screen.getByText("内容已经恢复。")).toBeInTheDocument();
    expect(client.getMessages).toHaveBeenCalledWith("s-1");
  });

  it("streams text, tool traces, and Skill notices into one assistant turn", async () => {
    const user = userEvent.setup();
    const client = makeClient({
      events: [
        { type: "skill_loaded", name: "research", already_loaded: false },
        { type: "tool_call", call_id: "call-1", name: "calculator", arguments: { expression: "2+2" } },
        { type: "tool_result", tool_call_id: "call-1", name: "calculator", result: { value: 4 } },
        { type: "text_delta", delta: "结果是 **4**。" },
        { type: "done", session_id: "new-session", finish_reason: "stop" },
      ],
    });
    render(<App client={client} renderWater={false} />);

    const textbox = screen.getByRole("textbox", { name: "消息" });
    await user.type(textbox, "@research calculate");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText(/结果是/)).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("calculator")).toBeInTheDocument();
    await waitFor(() => expect(client.streamChat).toHaveBeenCalledTimes(1));
    const body = vi.mocked(client.streamChat).mock.calls[0][0];
    expect(body).toEqual({
      session_id: "new-session",
      message: "@research calculate",
      skills: ["research"],
    });
  });

  it("offers Skills as accessible checkboxes in chat settings", async () => {
    const user = userEvent.setup();
    render(<App client={makeClient()} renderWater={false} />);

    await user.click(screen.getByRole("button", { name: "聊天设置" }));
    const dialog = screen.getByRole("dialog", { name: "聊天设置" });
    const research = within(dialog).getByRole("checkbox", { name: /research/ });
    await user.click(research);

    expect(research).toBeChecked();
    expect(screen.getByRole("button", { name: "移除 Skill research" })).toBeInTheDocument();
  });
});

describe("hydrateHistory", () => {
  it("pairs nested Agent tool metadata and preserves tool-role Skill records", () => {
    const history = hydrateHistory([
      {
        id: "assistant-call",
        role: "assistant",
        kind: "tool_call",
        content: "",
        metadata: {
          _agent: {
            tool_calls: [
              { id: "nested-call", name: "calculator", arguments: { expression: "2+2" } },
            ],
          },
        },
      },
      {
        id: "tool-result",
        role: "tool",
        kind: "tool_result",
        content: '{"result":{"value":4}}',
        metadata: { _agent: { tool_call_id: "nested-call" } },
      },
      {
        id: "skill-message",
        role: "tool",
        kind: "skill",
        content: "injected instructions",
        metadata: { skill_name: "research" },
      },
    ]);

    expect(history).toHaveLength(2);
    expect(history[0].traces).toEqual([
      expect.objectContaining({
        callId: "nested-call",
        name: "calculator",
        status: "success",
        result: { value: 4 },
      }),
    ]);
    expect(history[1]).toEqual(expect.objectContaining({ id: "skill-message", kind: "skill" }));
  });

  it("renders persisted summaries and removed Skills as compact notices", () => {
    render(
      <MessageList
        messages={[
          {
            id: "summary-message",
            role: "user",
            kind: "summary",
            content: "<conversation_summary>raw control text</conversation_summary>",
          },
          {
            id: "removed-skill",
            role: "tool",
            kind: "skill_removed",
            name: "research",
            content: "removed",
          },
        ]}
      />,
    );

    expect(
      screen.getByText("\u8f83\u65e9\u7684\u5bf9\u8bdd\u5df2\u538b\u7f29\u4e3a\u6458\u8981"),
    ).toBeInTheDocument();
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("\u5df2\u4ece\u4e0a\u4e0b\u6587\u79fb\u9664")).toBeInTheDocument();
    expect(screen.queryByText(/raw control text/)).not.toBeInTheDocument();
  });
});
