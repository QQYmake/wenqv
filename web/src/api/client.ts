import type {
  AgentEvent,
  ModelDiscoveryRequest,
  ModelDiscoveryResponse,
  ProviderConfigSet,
  PublicConfig,
  ReasoningEffort,
  RuntimeContext,
  Skill,
} from "../types";
import { parseSSEStream } from "./sse";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
let workspaceId = "";

export function resolveApiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path}`;
}

export function setWorkspaceId(value: string): void {
  workspaceId = value;
}

export function getWorkspaceHeader(): HeadersInit {
  if (!workspaceId) throw new ApiError("本地工作区尚未初始化", 0);
  return { "X-Workspace-ID": workspaceId };
}

class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    ...init,
    credentials: "omit",
    headers: {
      Accept: "application/json",
      ...getWorkspaceHeader(),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      message = body.detail ?? body.message ?? message;
    } catch {
      // Fixed status fallback when the proxy provides no JSON error body.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(`服务器返回了非 JSON 响应（HTTP ${response.status}）`, response.status);
  }
}

function asArray<T>(value: T[] | { [key: string]: T[] }, key: string): T[] {
  return Array.isArray(value) ? value : value[key] ?? [];
}

export const api = {
  async listSkills(): Promise<Skill[]> {
    return asArray(await request<Skill[] | { skills: Skill[] }>("/api/skills"), "skills");
  },

  getConfig(): Promise<PublicConfig> {
    return request("/api/config");
  },

  testProvider(provider_config: ProviderConfigSet): Promise<{ ok: boolean; roles: Record<string, { ok: boolean }> }> {
    return request("/api/provider/test", { method: "POST", body: JSON.stringify({ provider_config }) });
  },

  listModels(body: ModelDiscoveryRequest): Promise<ModelDiscoveryResponse> {
    return request("/api/provider/models", { method: "POST", body: JSON.stringify(body) });
  },

  abortChat(sessionId: string, requestId?: string): Promise<void> {
    return request("/api/chat/abort", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, ...(requestId ? { request_id: requestId } : {}) }),
    });
  },

  async *streamChat(
    body: {
      session_id: string;
      message: string;
      runtime_context: RuntimeContext;
      provider_config: ProviderConfigSet;
      reasoning_effort: ReasoningEffort;
      skills?: string[];
    },
    signal: AbortSignal,
  ): AsyncGenerator<AgentEvent> {
    const response = await fetch(resolveApiUrl("/api/chat"), {
      method: "POST",
      credentials: "omit",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json", ...getWorkspaceHeader() },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok || !response.body) {
      let message = `无法开始对话（${response.status}）`;
      try {
        const detail = (await response.json()) as { detail?: string; message?: string };
        message = detail.detail ?? detail.message ?? message;
      } catch {
        // Keep a useful fixed status message.
      }
      throw new ApiError(message, response.status);
    }
    for await (const frame of parseSSEStream(response.body)) {
      if (frame.data.trim() === "[DONE]") {
        yield { type: "done" };
        continue;
      }
      try {
        const payload = JSON.parse(frame.data) as Record<string, unknown>;
        yield { ...payload, type: String(payload.type ?? frame.event ?? "message") } as AgentEvent;
      } catch {
        yield { type: "error", code: "invalid_stream_event", message: "invalid_stream_event" };
      }
    }
  },
};

export type AgentApi = typeof api;
export { ApiError };
