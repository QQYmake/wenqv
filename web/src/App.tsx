import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, setWorkspaceId, type AgentApi } from "./api/client";
import { parseExportFileResult, triggerFileDownload } from "./api/download";
import { Composer } from "./components/Composer";
import { Icon } from "./components/Icon";
import { MessageList } from "./components/MessageList";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { Welcome } from "./components/Welcome";
import {
  createSession,
  deleteSession as deleteLocalSession,
  getWorkspaceId,
  listSessions,
  loadConversation,
  loadProviderConfig,
  LocalPrivacyError,
  renameSession as renameLocalSession,
  saveConversation,
} from "./storage/local";
import type { AgentEvent, ChatMessage, PublicConfig, ReasoningEffort, RuntimeContext, Session, Skill, Theme, ToolTrace } from "./types";

const LakeBackground = lazy(() => import("./scene/LakeBackground").then((module) => ({ default: module.LakeBackground })));
const THEME_KEY = "blue-lake.theme";
const REASONING_EFFORT_KEY = "blue-lake.reasoning-effort";

function uniqueId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* OS preference is enough when localStorage is blocked. */ }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function initialReasoningEffort(): ReasoningEffort {
  try {
    const stored = localStorage.getItem(REASONING_EFFORT_KEY);
    if (stored === "low" || stored === "medium" || stored === "high" || stored === "max") return stored;
  } catch { /* use the safe default */ }
  return "medium";
}

function emptyRuntimeContext(): RuntimeContext {
  return { messages: [], active_skills: [] };
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch { return value; }
}

function fallbackTitle(message: string): string {
  return message.replace(/(?<!\w)@[A-Za-z0-9][A-Za-z0-9_-]{0,63}\b/gu, "").replace(/\s+/gu, " ").trim().slice(0, 48).replace(/[ ,.;，。；]+$/u, "") || "新对话";
}

function isNewTitle(title: string): boolean {
  return ["", "new conversation", "new chat", "新对话"].includes(title.trim().toLocaleLowerCase());
}

function appendOptimisticUser(context: RuntimeContext, content: string, requestId: string): RuntimeContext {
  return {
    ...context,
    messages: [...context.messages, { role: "user", content, tool_calls: [], tool_call_id: null, name: null, metadata: { request_id: requestId } }],
  };
}

interface ConversationCache {
  messages: ChatMessage[];
  runtimeContext: RuntimeContext;
}

interface AppProps { client?: AgentApi; renderWater?: boolean; }

export function App({ client = api, renderWater = true }: AppProps) {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(initialReasoningEffort);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [config, setConfig] = useState<PublicConfig>({});
  const [modelId, setModelId] = useState("");
  const [draft, setDraft] = useState("");
  const [selectedSkills, setSelectedSkills] = useState<Set<string>>(new Set());
  const [sidebarExpanded, setSidebarExpanded] = useState(false);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [appError, setAppError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiConfigured, setApiConfigured] = useState(false);
  const [workspaceId, setLocalWorkspaceId] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const sendingRef = useRef(false);
  const activeSessionRef = useRef<string | null>(null);
  const cacheRef = useRef(new Map<string, ConversationCache>());
  const savesRef = useRef(new Map<string, Promise<void>>());
  const downloadedExportCallsRef = useRef(new Set<string>());

  useEffect(() => { activeSessionRef.current = activeSessionId; }, [activeSessionId]);
  useEffect(() => () => abortControllerRef.current?.abort(), []);

  const updateSession = useCallback((updated: Session | null) => {
    if (!updated) return;
    setSessions((current) => [updated, ...current.filter((session) => session.id !== updated.id)].sort((left, right) => String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? ""))));
  }, []);

  const persistConversation = useCallback((sessionId: string, value: ConversationCache) => {
    cacheRef.current.set(sessionId, value);
    if (activeSessionRef.current === sessionId) setMessages(value.messages);
    const previous = savesRef.current.get(sessionId) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(async () => updateSession(await saveConversation(sessionId, value.messages, value.runtimeContext)));
    savesRef.current.set(sessionId, next);
    void next.finally(() => { if (savesRef.current.get(sessionId) === next) savesRef.current.delete(sessionId); });
  }, [updateSession]);

  const refreshProviderState = useCallback(async (id: string) => {
    try {
      const provider = await loadProviderConfig(id);
      setApiConfigured(Boolean(provider));
      setModelId(provider?.main.model ?? "");
    } catch {
      setApiConfigured(false);
      setModelId("");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const id = await getWorkspaceId();
        if (!alive) return;
        setWorkspaceId(id);
        setLocalWorkspaceId(id);
        const [storedSessions, skillResult, configResult] = await Promise.all([
          listSessions(), client.listSkills().catch(() => []), client.getConfig().catch(() => ({})), refreshProviderState(id),
        ]);
        if (!alive) return;
        setSessions(storedSessions);
        setSkills(skillResult);
        setConfig(configResult);
        const first = storedSessions[0];
        if (first) setActiveSessionId(first.id);
      } catch (cause) {
        if (!alive) return;
        setAppError(cause instanceof LocalPrivacyError ? "此浏览器无法提供所需的本地加密存储。" : "无法初始化本地对话存储。");
      } finally { if (alive) setLoadingSessions(false); }
    };
    void load();
    return () => { alive = false; };
  }, [client, refreshProviderState]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#ede9dc" : "#061614");
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* non-sensitive preference only */ }
  }, [theme]);
  useEffect(() => { try { localStorage.setItem(REASONING_EFFORT_KEY, reasoningEffort); } catch { /* one-tab setting remains */ } }, [reasoningEffort]);

  useEffect(() => {
    if (!activeSessionId) { setMessages([]); return; }
    const cached = cacheRef.current.get(activeSessionId);
    if (cached) { setMessages(cached.messages); return; }
    let alive = true;
    setLoadingMessages(true);
    void loadConversation(activeSessionId).then((conversation) => {
      if (!alive) return;
      const value = { messages: conversation.messages, runtimeContext: conversation.runtimeContext };
      cacheRef.current.set(activeSessionId, value);
      setMessages(value.messages);
    }).catch(() => { if (alive) setAppError("无法恢复本地对话。"); }).finally(() => { if (alive) setLoadingMessages(false); });
    return () => { alive = false; };
  }, [activeSessionId]);

  const stopCurrentStream = useCallback(() => {
    const sessionId = activeSessionRef.current;
    if (sessionId && config.features?.abort !== false) void client.abortChat(sessionId).catch(() => undefined);
    abortControllerRef.current?.abort();
  }, [client, config.features?.abort]);

  const beginNewConversation = () => {
    if (streaming) stopCurrentStream();
    setActiveSessionId(null); setDraft(""); setSelectedSkills(new Set()); setSidebarExpanded(false);
  };
  const selectSession = (id: string) => {
    if (id === activeSessionRef.current) return setSidebarExpanded(false);
    if (streaming) stopCurrentStream();
    setActiveSessionId(id); setSidebarExpanded(false);
  };
  const renameSession = async (id: string, title: string) => {
    try { updateSession(await renameLocalSession(id, title)); } catch { setAppError("重命名失败"); }
  };
  const deleteSession = async (id: string) => {
    try {
      await deleteLocalSession(id); cacheRef.current.delete(id); setSessions((current) => current.filter((session) => session.id !== id));
      if (id === activeSessionRef.current) beginNewConversation();
    } catch { setAppError("删除失败"); }
  };
  const updateAssistant = (sessionId: string, assistantId: string, updater: (message: ChatMessage) => ChatMessage) => {
    const current = cacheRef.current.get(sessionId);
    if (!current) return;
    persistConversation(sessionId, { ...current, messages: current.messages.map((message) => message.id === assistantId ? updater(message) : message) });
  };
  const handleAgentEvent = (event: AgentEvent, sessionId: string, assistantId: string) => {
    if (event.type === "conversation_state") {
      const current = cacheRef.current.get(sessionId);
      const runtimeContext = (event as { runtime_context: RuntimeContext }).runtime_context;
      if (current) persistConversation(sessionId, { ...current, runtimeContext });
      return;
    }
    if (event.type === "reasoning_delta") return updateAssistant(sessionId, assistantId, (message) => ({ ...message, reasoningSummary: `${message.reasoningSummary ?? ""}${String(event.delta ?? "")}` }));
    if (event.type === "text_delta") return updateAssistant(sessionId, assistantId, (message) => ({ ...message, content: message.content + String(event.delta ?? "") }));
    if (event.type === "tool_call") return updateAssistant(sessionId, assistantId, (message) => ({ ...message, traces: [...(message.traces ?? []), { callId: String(event.call_id), name: String(event.name), arguments: parseMaybeJson(event.arguments), status: "running" } satisfies ToolTrace] }));
    if (event.type === "tool_result") {
      const callId = String(event.call_id ?? event.tool_call_id ?? uniqueId("tool"));
      if (event.name === "export_file" && !event.error && !downloadedExportCallsRef.current.has(callId)) {
        const file = parseExportFileResult(parseMaybeJson(event.result));
        if (file) { downloadedExportCallsRef.current.add(callId); void triggerFileDownload(file).catch(() => setAppError("文件下载失败")); }
      }
      return updateAssistant(sessionId, assistantId, (message) => {
        let found = false;
        const traces = (message.traces ?? []).map((trace) => {
          if (trace.callId !== callId) return trace;
          found = true;
          return { ...trace, name: String(event.name ?? trace.name), result: parseMaybeJson(event.result), error: Boolean(event.error), truncated: Boolean(event.truncated), patch: typeof event.patch === "string" ? event.patch : undefined, patchTruncated: Boolean(event.patch_truncated), status: event.error ? "error" : "success" } satisfies ToolTrace;
        });
        if (!found) traces.push({ callId, name: String(event.name ?? "tool"), result: parseMaybeJson(event.result), error: Boolean(event.error), status: event.error ? "error" : "success" });
        return { ...message, traces };
      });
    }
    if (event.type === "skill_loaded") return updateAssistant(sessionId, assistantId, (message) => {
      const name = String(event.name); if (message.skills?.some((notice) => notice.name === name)) return message;
      return { ...message, skills: [...(message.skills ?? []), { name, alreadyLoaded: Boolean(event.already_loaded) }] };
    });
    if (event.type === "error") return updateAssistant(sessionId, assistantId, (message) => ({ ...message, error: String(event.message), pending: false }));
    if (event.type === "done") return updateAssistant(sessionId, assistantId, (message) => ({ ...message, pending: false, content: message.content || (event.finish_reason === "max_turns" ? "已达到本次任务的最大执行轮次。你可以缩小范围后继续。" : message.content) }));
  };

  const sendMessage = async (rawMessage: string) => {
    const content = rawMessage.trim();
    if (!content || streaming || sendingRef.current || loadingMessages || !workspaceId) return;
    sendingRef.current = true; setAppError(null);
    let sessionId = activeSessionRef.current;
    try {
      if (!sessionId) {
        const created = await createSession();
        sessionId = created.id; cacheRef.current.set(sessionId, { messages: [], runtimeContext: emptyRuntimeContext() });
        updateSession(created); setActiveSessionId(sessionId);
      }
      const current = cacheRef.current.get(sessionId) ?? { messages: [], runtimeContext: emptyRuntimeContext() };
      const provider = await loadProviderConfig(workspaceId);
      if (!provider) throw new LocalPrivacyError("provider_reconfigure");
      const mentioned = [...content.matchAll(/@([\w-]+)/gu)].map((match) => match[1]).filter((name) => skills.some((skill) => skill.name === name));
      const requestedSkills = [...new Set([...selectedSkills, ...mentioned])];
      const requestId = uniqueId("request");
      const userMessage: ChatMessage = { id: uniqueId("user"), session_id: sessionId, role: "user", content };
      const assistantId = uniqueId("assistant");
      const assistantMessage: ChatMessage = { id: assistantId, session_id: sessionId, role: "assistant", content: "", traces: [], skills: [], pending: true, reasoningEffort };
      const optimistic = { messages: [...current.messages, userMessage, assistantMessage], runtimeContext: appendOptimisticUser(current.runtimeContext, content, requestId) };
      persistConversation(sessionId, optimistic);
      const active = sessions.find((session) => session.id === sessionId);
      if (active && isNewTitle(active.title)) updateSession(await renameLocalSession(sessionId, fallbackTitle(content)));
      setDraft(""); setSelectedSkills(new Set()); setStreaming(true);
      const controller = new AbortController(); abortControllerRef.current = controller;
      let reachedDone = false;
      try {
        for await (const event of client.streamChat({ session_id: sessionId, message: content, runtime_context: current.runtimeContext, provider_config: provider, reasoning_effort: reasoningEffort, ...(requestedSkills.length ? { skills: requestedSkills } : {}) }, controller.signal)) {
          handleAgentEvent(event, sessionId, assistantId);
          if (event.type === "done") reachedDone = true;
        }
        if (!reachedDone) updateAssistant(sessionId, assistantId, (message) => ({ ...message, pending: false }));
      } catch (cause) {
        updateAssistant(sessionId, assistantId, (message) => ({ ...message, pending: false, content: cause instanceof DOMException && cause.name === "AbortError" ? message.content || "已停止本次生成。" : message.content, error: cause instanceof DOMException && cause.name === "AbortError" ? undefined : "对话流意外中断" }));
      } finally { abortControllerRef.current = null; setStreaming(false); }
    } catch (cause) {
      if (cause instanceof LocalPrivacyError) { setApiConfigured(false); setSettingsOpen(true); setAppError("请重新填写 API 配置后再继续。"); }
      else setAppError("无法开始对话。");
    } finally { sendingRef.current = false; }
  };

  const activeSession = useMemo(() => sessions.find((session) => session.id === activeSessionId), [sessions, activeSessionId]);
  const isConversation = Boolean(activeSessionId && (messages.length || loadingMessages));
  return <div className="app" data-theme={theme}>
    <a className="skip-link" href="#conversation-stage">跳到对话区</a>
    {renderWater && <Suspense fallback={<div className="lake-background lake-background--static" aria-hidden="true" />}><LakeBackground theme={theme} /></Suspense>}
    <div className="atmosphere" aria-hidden="true" />
    <Sidebar sessions={sessions} activeSessionId={activeSessionId} expanded={sidebarExpanded} loading={loadingSessions} theme={theme} apiConfigured={apiConfigured} onExpand={() => setSidebarExpanded(true)} onNew={beginNewConversation} onSelect={selectSession} onRename={renameSession} onDelete={deleteSession} onThemeChange={setTheme} onOpenSettings={() => setSettingsOpen(true)} />
    {sidebarExpanded && <button className="sidebar-scrim" onClick={() => setSidebarExpanded(false)} aria-label="关闭侧栏" />}
    <main className={`workspace${isConversation ? " workspace--conversation" : " workspace--welcome"}`}><header className="workspace-header"><button className="mobile-menu" onClick={() => setSidebarExpanded(true)} aria-label="打开侧栏"><Icon name="menu" /></button><div className="header-title"><span>{activeSession?.title ?? "问渠"}</span>{streaming && <small><i /> 正在工作</small>}</div><span className="header-watermark">问渠</span></header>
      <div className="conversation-stage" id="conversation-stage" tabIndex={-1}>{loadingMessages ? <div className="history-loading" role="status"><span /><p>正在拾起这段对话…</p></div> : isConversation ? <MessageList messages={messages} /> : <Welcome onSuggestion={(prompt) => { setDraft(prompt); requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="消息"]')?.focus()); }} />}
        <div className="composer-dock"><Composer value={draft} modelId={modelId} skills={skills} selectedSkills={selectedSkills} streaming={streaming} abortEnabled={config.features?.abort !== false} apiConfigured={apiConfigured} reasoningEffort={reasoningEffort} onChange={setDraft} onSubmit={(value) => void sendMessage(value)} onAbort={stopCurrentStream} onOpenSettings={() => setSettingsOpen(true)} onReasoningEffortChange={setReasoningEffort} onToggleSkill={(name, selected) => setSelectedSkills((current) => { const next = new Set(current); selected ? next.add(name) : next.delete(name); return next; })} /></div>
      </div>
    </main>
    {appError && <div className="toast" role="alert"><span>{appError}</span><button onClick={() => setAppError(null)} aria-label="关闭提示"><Icon name="close" /></button></div>}
    {settingsOpen && workspaceId && <SettingsPanel client={client} workspaceId={workspaceId} onClose={() => setSettingsOpen(false)} onConfigSaved={(nextModel) => { setApiConfigured(true); setModelId(nextModel); }} />}
  </div>;
}
