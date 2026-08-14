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
        { type: "reasoning_delta", delta: "先检查工具结果。" },
        { type: "skill_loaded", name: "research", already_loaded: false },
        { type: "tool_call", call_id: "call-1", name: "calculator", arguments: { expression: "2+2" } },
        {
          type: "tool_result",
          tool_call_id: "call-1",
          name: "calculator",
          result: { value: 4 },
          patch: "--- a/note.txt\n+++ b/note.txt\n-old\n+new\n",
        },
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
    expect(screen.getByText("修改差异")).toBeInTheDocument();
    const reasoningLabel = screen.getByText("思考中");
    const reasoningDetails = reasoningLabel.closest("details");
    expect(reasoningDetails).not.toHaveAttribute("open");
    await user.click(reasoningLabel);
    expect(reasoningDetails).toHaveAttribute("open");
    expect(screen.getByText("先检查工具结果。")).toBeInTheDocument();
    await user.click(reasoningLabel);
    expect(reasoningDetails).not.toHaveAttribute("open");
    await waitFor(() => expect(client.streamChat).toHaveBeenCalledTimes(1));
    const body = vi.mocked(client.streamChat).mock.calls[0][0];
    expect(body).toEqual({
      session_id: "new-session",
      message: "@research calculate",
      reasoning_effort: "medium",
      skills: ["research"],
    });
  });

  it("remembers the per-turn reasoning effort and keeps the marker without a summary", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    const { unmount } = render(<App client={client} renderWater={false} />);

    const effort = screen.getByRole("combobox", { name: "思考强度" });
    expect(effort).toHaveValue("medium");
    await user.selectOptions(effort, "max");
    expect(localStorage.getItem("blue-lake.reasoning-effort")).toBe("max");

    await user.type(screen.getByRole("textbox", { name: "消息" }), "认真回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(client.streamChat).toHaveBeenCalledTimes(1));
    expect(vi.mocked(client.streamChat).mock.calls[0][0]).toEqual({
      session_id: "new-session",
      message: "认真回答",
      reasoning_effort: "max",
    });
    expect(screen.getByText("思考中")).toBeInTheDocument();

    unmount();
    render(<App client={makeClient()} renderWater={false} />);
    expect(screen.getByRole("combobox", { name: "思考强度" })).toHaveValue("max");
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

  it("merges one ReAct request into one restored assistant turn", () => {
    const history = hydrateHistory([
      {
        id: "assistant-call-1",
        role: "assistant",
        kind: "tool_call",
        content: "",
        metadata: {
          request_id: "run-1",
          reasoning_effort: "high",
          reasoning_summary: "先查数据。",
          _agent: {
            tool_calls: [{ id: "call-1", name: "search", arguments: { q: "lake" } }],
          },
        },
      },
      {
        id: "tool-result-1",
        role: "tool",
        kind: "tool_result",
        content: '{"result":{"hits":2}}',
        metadata: { _agent: { tool_call_id: "call-1" } },
      },
      {
        id: "assistant-call-2",
        role: "assistant",
        kind: "tool_call",
        content: "",
        metadata: {
          request_id: "run-1",
          reasoning_effort: "high",
          reasoning_summary: "再核对。",
          _agent: {
            tool_calls: [{ id: "call-2", name: "calculator", arguments: { expression: "1+1" } }],
          },
        },
      },
      {
        id: "tool-result-2",
        role: "tool",
        kind: "tool_result",
        content: '{"result":{"value":2}}',
        metadata: { _agent: { tool_call_id: "call-2" } },
      },
      {
        id: "assistant-final",
        role: "assistant",
        content: "最终答案。",
        metadata: {
          request_id: "run-1",
          reasoning_effort: "high",
          reasoning_summary: "完成收束。",
        },
      },
    ]);

    expect(history).toHaveLength(1);
    expect(history[0]).toEqual(
      expect.objectContaining({
        content: "最终答案。",
        reasoningEffort: "high",
        reasoningSummary: "先查数据。\n\n再核对。\n\n完成收束。",
      }),
    );
    expect(history[0].traces).toEqual([
      expect.objectContaining({ callId: "call-1", status: "success" }),
      expect.objectContaining({ callId: "call-2", status: "success" }),
    ]);
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

  it("restores the permanent reasoning marker even when no summary was returned", () => {
    const [message] = hydrateHistory([
      {
        id: "assistant-final",
        role: "assistant",
        content: "回答。",
        metadata: {
          request_id: "run-empty",
          reasoning_effort: "medium",
          reasoning_summary: "",
        },
      },
    ]);

    render(<MessageList messages={[message]} />);
    expect(screen.getByText("思考中")).toBeInTheDocument();
    expect(screen.getByText("回答。")).toBeInTheDocument();
  });
});
