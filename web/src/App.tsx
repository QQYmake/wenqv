import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type AgentApi } from "./api/client";
import { Composer } from "./components/Composer";
import { Icon } from "./components/Icon";
import { MessageList } from "./components/MessageList";
import { Sidebar } from "./components/Sidebar";
import { SettingsPanel } from "./components/SettingsPanel";
import { Welcome } from "./components/Welcome";
import type { AgentEvent, ChatMessage, PublicConfig, Session, Skill, Theme, ToolTrace } from "./types";

const LakeBackground = lazy(() =>
  import("./scene/LakeBackground").then((module) => ({ default: module.LakeBackground })),
);

const THEME_KEY = "blue-lake.theme";
const SESSION_KEY = "blue-lake.active-session";
const WORKSPACE_KEY = "blue-lake.workspace-id";

function uniqueId(prefix: string) {
  const uuid = globalThis.crypto?.randomUUID?.();
  return uuid ? `${prefix}-${uuid}` : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Storage can be disabled; the OS preference remains a safe fallback.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function metadataValue(message: ChatMessage, key: string) {
  return message.metadata?.[key];
}

function nestedAgentMetadata(message: ChatMessage): Record<string, unknown> {
  const value = message.metadata?._agent;
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

export function hydrateHistory(messages: ChatMessage[]): ChatMessage[] {
  const display: ChatMessage[] = [];
  const traceOwners = new Map<string, ChatMessage>();

  for (const message of messages) {
    const kind = message.kind?.toLocaleLowerCase() ?? "";
    const payload = parseMaybeJson(message.content);
    const objectPayload =
      payload && typeof payload === "object" ? (payload as Record<string, unknown>) : undefined;
    const agentMetadata = nestedAgentMetadata(message);
    const callId = String(
      metadataValue(message, "call_id") ??
        agentMetadata.tool_call_id ??
        objectPayload?.call_id ??
        objectPayload?.tool_call_id ??
        message.id,
    );

    if (kind === "skill" || kind === "skill_injection" || kind === "skill_removed") {
      display.push(message);
      continue;
    }

    if (kind === "tool_call") {
      const storedCalls = Array.isArray(agentMetadata.tool_calls)
        ? agentMetadata.tool_calls.filter(
            (item): item is Record<string, unknown> => Boolean(item && typeof item === "object"),
          )
        : [];
      const traces: ToolTrace[] =
        storedCalls.length > 0
          ? storedCalls.map((call, index) => ({
              callId: String(call.id ?? `${message.id}-${index}`),
              name: String(call.name ?? "tool"),
              arguments: call.arguments,
              status: "running",
            }))
          : [
              {
                callId,
                name: String(
                  message.name ?? metadataValue(message, "name") ?? objectPayload?.name ?? "tool",
                ),
                arguments:
                  metadataValue(message, "arguments") ??
                  objectPayload?.arguments ??
                  objectPayload?.input ??
                  payload,
                status: "running",
              },
            ];
      const traceMessage: ChatMessage = {
        ...message,
        role: "assistant",
        content: "",
        traces,
      };
      display.push(traceMessage);
      traces.forEach((trace) => traceOwners.set(trace.callId, traceMessage));
      continue;
    }

    if (message.role === "tool" || kind === "tool_result") {
      const owner = traceOwners.get(callId);
      if (owner?.traces) {
        const error = Boolean(metadataValue(message, "error") ?? objectPayload?.error);
        owner.traces = owner.traces.map((trace) =>
          trace.callId === callId
            ? {
                ...trace,
                result: objectPayload?.result ?? payload,
                error,
                truncated: Boolean(metadataValue(message, "truncated") ?? objectPayload?.truncated),
                status: error ? "error" : "success",
              }
            : trace,
        );
      }
      continue;
    }

    display.push(message);
  }

  return display;
}

interface AppProps {
  client?: AgentApi;
  renderWater?: boolean;
}

export function App({ client = api, renderWater = true }: AppProps) {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [config, setConfig] = useState<PublicConfig>({ model_id: "main" });
  const [draft, setDraft] = useState("");
  const [selectedSkills, setSelectedSkills] = useState<Set<string>>(new Set());
  const [sidebarExpanded, setSidebarExpanded] = useState(false);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [appError, setAppError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiConfigured, setApiConfigured] = useState(true);
  const abortControllerRef = useRef<AbortController | null>(null);
  const sendingRef = useRef(false);
  const skipSessionLoadRef = useRef<string | null>(null);
  const activeSessionRef = useRef<string | null>(null);

  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => () => abortControllerRef.current?.abort(), []);

  const refreshConfiguredState = useCallback(async () => {
    // Re-read the server's view of the config so the composer unlocks right
    // after a config is saved in Settings (the initial load may have run
    // before anything was configured).
    const configResult = await client.getConfig().catch(() => null);
    const userConfigResult =
      typeof client.getUserConfig === "function"
        ? await client.getUserConfig().catch(() => null)
        : null;
    if (configResult) setConfig(configResult);
    const hasUserConfig = Boolean(userConfigResult?.has_config);
    const hasDefault = Boolean(configResult?.model_id);
    setApiConfigured(hasUserConfig || hasDefault);
  }, [client]);

  const refreshSessions = useCallback(async () => {
    const next = await client.listSessions();
    setSessions(next);
    return next;
  }, [client]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      // Issue (or refresh) the workspace identity cookie first so the
      // AuthMiddleware has a workspace_id for every following request. The
      // cookie is HttpOnly; we keep a non-sensitive localStorage mirror only
      // so the UI can show which workspace is active.
      if (typeof client.bootstrap === "function") {
        try {
          const { workspace_id } = await client.bootstrap();
          if (alive) {
            try {
              localStorage.setItem(WORKSPACE_KEY, workspace_id);
            } catch {
              // Storage may be unavailable; the cookie is still the source of truth.
            }
          }
        } catch {
          // Bootstrap is best-effort; header-based identity (tests) still works.
        }
      }

      const [sessionResult, skillResult, configResult, userConfigResult] =
        await Promise.allSettled([
          client.listSessions(),
          client.listSkills(),
          client.getConfig(),
          typeof client.getUserConfig === "function"
            ? client.getUserConfig()
            : Promise.reject(new Error("no user config endpoint")),
        ]);
      if (!alive) return;

      if (sessionResult.status === "fulfilled") {
        setSessions(sessionResult.value);
        try {
          const remembered = localStorage.getItem(SESSION_KEY);
          if (remembered && sessionResult.value.some((session) => session.id === remembered)) {
            setActiveSessionId(remembered);
          }
        } catch {
          // Session remembrance is optional.
        }
      }
      if (skillResult.status === "fulfilled") setSkills(skillResult.value);
      if (configResult.status === "fulfilled") setConfig(configResult.value);
      if (userConfigResult.status === "fulfilled") {
        const hasUserConfig = Boolean(userConfigResult.value.has_config);
        const hasDefault =
          configResult.status === "fulfilled" && Boolean(configResult.value.model_id);
        setApiConfigured(hasUserConfig || hasDefault);
      } else if (configResult.status === "fulfilled") {
        // No user-config endpoint (e.g. mocked clients): fall back to whether a
        // default model id is published by the server.
        setApiConfigured(Boolean(configResult.value.model_id));
      }

      const failure = [sessionResult, skillResult, configResult, userConfigResult].find(
        (result): result is PromiseRejectedResult => result.status === "rejected",
      );
      if (failure) setAppError(failure.reason instanceof Error ? failure.reason.message : "部分数据暂时无法读取");
      setLoadingSessions(false);
    };
    void load();
    return () => {
      alive = false;
    };
  }, [client]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      "content",
      theme === "light" ? "#ede9dc" : "#061614",
    );
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Theme still works for this tab when persistent storage is unavailable.
    }
  }, [theme]);

  useEffect(() => {
    if (!activeSessionId) return;
    try {
      localStorage.setItem(SESSION_KEY, activeSessionId);
    } catch {
      // Session remembrance is optional.
    }

    if (skipSessionLoadRef.current === activeSessionId) {
      skipSessionLoadRef.current = null;
      return;
    }

    let alive = true;
    setLoadingMessages(true);
    client
      .getMessages(activeSessionId)
      .then((history) => {
        if (alive) setMessages(hydrateHistory(history));
      })
      .catch((error: unknown) => {
        if (alive) setAppError(error instanceof Error ? error.message : "无法恢复对话");
      })
      .finally(() => {
        if (alive) setLoadingMessages(false);
      });
    return () => {
      alive = false;
    };
  }, [activeSessionId, client]);

  const stopCurrentStream = useCallback(() => {
    const sessionId = activeSessionRef.current;
    if (sessionId && config.features?.abort !== false) void client.abortChat(sessionId).catch(() => undefined);
    abortControllerRef.current?.abort();
  }, [client, config.features?.abort]);

  const beginNewConversation = () => {
    if (streaming) stopCurrentStream();
    setActiveSessionId(null);
    setMessages([]);
    setDraft("");
    setSelectedSkills(new Set());
    setSidebarExpanded(false);
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {
      // Nothing else needs to happen.
    }
  };

  const selectSession = (id: string) => {
    if (id === activeSessionId) {
      setSidebarExpanded(false);
      return;
    }
    if (streaming) stopCurrentStream();
    setMessages([]);
    setActiveSessionId(id);
    setSidebarExpanded(false);
  };

  const renameSession = async (id: string, title: string) => {
    try {
      const updated = await client.renameSession(id, title);
      setSessions((current) => current.map((session) => (session.id === id ? updated : session)));
    } catch (error) {
      setAppError(error instanceof Error ? error.message : "重命名失败");
    }
  };

  const deleteSession = async (id: string) => {
    try {
      await client.deleteSession(id);
      setSessions((current) => current.filter((session) => session.id !== id));
      if (id === activeSessionRef.current) beginNewConversation();
    } catch (error) {
      setAppError(error instanceof Error ? error.message : "删除失败");
    }
  };

  const updateAssistant = (assistantId: string, updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((current) =>
      current.map((message) => (message.id === assistantId ? updater(message) : message)),
    );
  };

  const handleAgentEvent = (event: AgentEvent, assistantId: string) => {
    switch (event.type) {
      case "text_delta":
        updateAssistant(assistantId, (message) => ({
          ...message,
          content: message.content + String(event.delta ?? ""),
        }));
        break;
      case "tool_call": {
        const trace: ToolTrace = {
          callId: String(event.call_id),
          name: String(event.name),
          arguments: parseMaybeJson(event.arguments),
          status: "running",
        };
        updateAssistant(assistantId, (message) => ({
          ...message,
          traces: [...(message.traces ?? []), trace],
        }));
        break;
      }
      case "tool_result":
        updateAssistant(assistantId, (message) => {
          const callId = String(event.call_id ?? event.tool_call_id ?? uniqueId("tool"));
          let found = false;
          const traces = (message.traces ?? []).map((trace) => {
            if (trace.callId !== callId) return trace;
            found = true;
            return {
              ...trace,
              name: String(event.name ?? trace.name),
              result: event.result,
              error: Boolean(event.error),
              truncated: Boolean(event.truncated),
              status: event.error ? "error" : "success",
            } as ToolTrace;
          });
          if (!found) {
            traces.push({
              callId,
              name: String(event.name ?? "tool"),
              result: event.result,
              error: Boolean(event.error),
              truncated: Boolean(event.truncated),
              status: event.error ? "error" : "success",
            });
          }
          return { ...message, traces };
        });
        break;
      case "skill_loaded":
        updateAssistant(assistantId, (message) => {
          const notices = message.skills ?? [];
          const name = String(event.name);
          if (notices.some((notice) => notice.name === name)) return message;
          return {
            ...message,
            skills: [...notices, { name, alreadyLoaded: Boolean(event.already_loaded) }],
          };
        });
        break;
      case "error":
        updateAssistant(assistantId, (message) => ({ ...message, error: String(event.message) }));
        break;
      case "done":
        updateAssistant(assistantId, (message) => ({
          ...message,
          pending: false,
          content:
            message.content ||
            (event.finish_reason === "max_turns"
              ? "已达到本次任务的最大执行轮次。你可以缩小范围后继续。"
              : message.content),
        }));
        break;
    }
  };

  const sendMessage = async (rawMessage: string) => {
    const content = rawMessage.trim();
    if (!content || streaming || sendingRef.current) return;
    sendingRef.current = true;
    setAppError(null);

    let sessionId = activeSessionRef.current;
    try {
      if (!sessionId) {
        const created = await client.createSession();
        sessionId = created.id;
        skipSessionLoadRef.current = created.id;
        activeSessionRef.current = created.id;
        setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
        setActiveSessionId(created.id);
      }

      const mentioned = [...content.matchAll(/@([\w-]+)/gu)]
        .map((match) => match[1])
        .filter((name) => skills.some((skill) => skill.name === name));
      const requestedSkills = [...new Set([...selectedSkills, ...mentioned])];
      const userMessage: ChatMessage = {
        id: uniqueId("user"),
        session_id: sessionId,
        role: "user",
        content,
      };
      const assistantId = uniqueId("assistant");
      const assistantMessage: ChatMessage = {
        id: assistantId,
        session_id: sessionId,
        role: "assistant",
        content: "",
        traces: [],
        skills: [],
        pending: true,
      };

      setMessages((current) => [...current, userMessage, assistantMessage]);
      setDraft("");
      setSelectedSkills(new Set());
      setStreaming(true);

      const controller = new AbortController();
      abortControllerRef.current = controller;
      let reachedDone = false;

      try {
        for await (const event of client.streamChat(
          {
            session_id: sessionId,
            message: content,
            ...(requestedSkills.length > 0 ? { skills: requestedSkills } : {}),
          },
          controller.signal,
        )) {
          handleAgentEvent(event, assistantId);
          if (event.type === "done") reachedDone = true;
        }
        if (!reachedDone) {
          updateAssistant(assistantId, (message) => ({ ...message, pending: false }));
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          updateAssistant(assistantId, (message) => ({
            ...message,
            pending: false,
            content: message.content || "已停止本次生成。",
          }));
        } else {
          updateAssistant(assistantId, (message) => ({
            ...message,
            pending: false,
            error: error instanceof Error ? error.message : "对话流意外中断",
          }));
        }
      } finally {
        sendingRef.current = false;
        abortControllerRef.current = null;
        setStreaming(false);
        void refreshSessions().catch(() => undefined);
      }
    } catch (error) {
      sendingRef.current = false;
      setAppError(error instanceof Error ? error.message : "无法创建对话");
    }
  };

  const activeSession = useMemo(
    () => sessions.find((session) => session.id === activeSessionId),
    [activeSessionId, sessions],
  );
  const isConversation = Boolean(activeSessionId && (messages.length > 0 || loadingMessages));

  return (
    <div className="app" data-theme={theme}>
      <a className="skip-link" href="#conversation-stage">跳到对话区</a>
      {renderWater && (
        <Suspense fallback={<div className="lake-background lake-background--static" aria-hidden="true" />}>
          <LakeBackground theme={theme} />
        </Suspense>
      )}
      <div className="atmosphere" aria-hidden="true" />

      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        expanded={sidebarExpanded}
        loading={loadingSessions}
        theme={theme}
        apiConfigured={apiConfigured}
        onExpand={() => setSidebarExpanded(true)}
        onNew={beginNewConversation}
        onSelect={selectSession}
        onRename={renameSession}
        onDelete={deleteSession}
        onThemeChange={setTheme}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      {sidebarExpanded && (
        <button className="sidebar-scrim" onClick={() => setSidebarExpanded(false)} aria-label="关闭侧栏" />
      )}

      <main className={`workspace${isConversation ? " workspace--conversation" : " workspace--welcome"}`}>
        <header className="workspace-header">
          <button className="mobile-menu" onClick={() => setSidebarExpanded(true)} aria-label="打开侧栏">
            <Icon name="menu" />
          </button>
          <div className="header-title">
            <span>{activeSession?.title ?? "湖心"}</span>
            {streaming && <small><i /> 正在工作</small>}
          </div>
          <span className="header-watermark">BLUE LAKE</span>
        </header>

        <div className="conversation-stage" id="conversation-stage" tabIndex={-1}>
          {loadingMessages ? (
            <div className="history-loading" role="status">
              <span />
              <p>正在拾起这段对话…</p>
            </div>
          ) : isConversation ? (
            <MessageList messages={messages} />
          ) : (
            <Welcome
              onSuggestion={(prompt) => {
                setDraft(prompt);
                requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="消息"]')?.focus());
              }}
            />
          )}

          <div className="composer-dock">
            <Composer
              value={draft}
              modelId={config.model_id}
              skills={skills}
              selectedSkills={selectedSkills}
              streaming={streaming}
              abortEnabled={config.features?.abort !== false}
              apiConfigured={apiConfigured}
              onChange={setDraft}
              onSubmit={(value) => void sendMessage(value)}
              onAbort={stopCurrentStream}
              onOpenSettings={() => setSettingsOpen(true)}
              onToggleSkill={(name, selected) =>
                setSelectedSkills((current) => {
                  const next = new Set(current);
                  if (selected) next.add(name);
                  else next.delete(name);
                  return next;
                })
              }
            />
          </div>
        </div>
      </main>

      {appError && (
        <div className="toast" role="alert">
          <span>{appError}</span>
          <button onClick={() => setAppError(null)} aria-label="关闭提示"><Icon name="close" /></button>
        </div>
      )}

      {settingsOpen && (
        <SettingsPanel
          client={client}
          onClose={() => setSettingsOpen(false)}
          onConfigSaved={() => void refreshConfiguredState()}
        />
      )}
    </div>
  );
}
