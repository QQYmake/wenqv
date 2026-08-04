import type { AgentEvent, ChatMessage, PublicConfig, Session, Skill } from "../types";
import { parseSSEStream } from "./sse";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
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
      // Keep the status-based message when an error response has no JSON body.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function asArray<T>(value: T[] | { [key: string]: T[] }, key: string): T[] {
  if (Array.isArray(value)) return value;
  return value[key] ?? [];
}

function normalizeMessage(raw: ChatMessage): ChatMessage {
  const supportedRoles = new Set(["user", "assistant", "system", "tool"]);
  return {
    ...raw,
    id: String(raw.id),
    role: supportedRoles.has(raw.role) ? raw.role : "system",
    content: typeof raw.content === "string" ? raw.content : String(raw.content ?? ""),
  };
}

export const api = {
  async listSessions(): Promise<Session[]> {
    const body = await request<Session[] | { sessions: Session[] }>("/api/sessions");
    return asArray(body, "sessions");
  },

  createSession(title = "新对话"): Promise<Session> {
    return request("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
  },

  renameSession(id: string, title: string): Promise<Session> {
    return request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  deleteSession(id: string): Promise<void> {
    return request(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async getMessages(id: string): Promise<ChatMessage[]> {
    const body = await request<ChatMessage[] | { messages: ChatMessage[] }>(
      `/api/sessions/${encodeURIComponent(id)}/messages`,
    );
    return asArray(body, "messages").map(normalizeMessage);
  },

  async listSkills(): Promise<Skill[]> {
    const body = await request<Skill[] | { skills: Skill[] }>("/api/skills");
    return asArray(body, "skills");
  },

  getConfig(): Promise<PublicConfig> {
    return request("/api/config");
  },

  async bootstrap(): Promise<{ workspace_id: string }> {
    return request("/api/bootstrap");
  },

  abortChat(sessionId: string): Promise<void> {
    return request("/api/chat/abort", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
  },

  async *streamChat(
    body: { session_id: string; message: string; skills?: string[] },
    signal: AbortSignal,
  ): AsyncGenerator<AgentEvent> {
    const response = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal,
    });

    if (!response.ok || !response.body) {
      let message = `无法开始对话（${response.status}）`;
      try {
        const detail = (await response.json()) as { detail?: string; message?: string };
        message = detail.detail ?? detail.message ?? message;
      } catch {
        // The generic status message remains useful for non-JSON failures.
      }
      throw new ApiError(message, response.status);
    }

    for await (const frame of parseSSEStream(response.body)) {
      if (frame.data.trim() === "[DONE]") {
        yield { type: "done" };
        continue;
      }

      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(frame.data) as Record<string, unknown>;
      } catch {
        payload = { delta: frame.data };
      }

      const type = String(payload.type ?? frame.event ?? "message");
      yield { ...payload, type } as AgentEvent;
    }
  },
};

export type AgentApi = typeof api;
export { ApiError };
