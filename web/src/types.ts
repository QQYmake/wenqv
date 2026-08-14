export type Theme = "light" | "dark";
export type ReasoningEffort = "low" | "medium" | "high" | "max";

export interface Session {
  id: string;
  workspace_id?: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
}

export interface Skill {
  name: string;
  description: string;
}

export interface ToolTrace {
  callId: string;
  name: string;
  arguments?: unknown;
  result?: unknown;
  error?: boolean;
  truncated?: boolean;
  patch?: string;
  patchTruncated?: boolean;
  status: "running" | "success" | "error";
}

export interface SkillNotice {
  name: string;
  alreadyLoaded?: boolean;
  removed?: boolean;
}

export interface ChatMessage {
  id: string;
  session_id?: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  kind?: string;
  name?: string;
  metadata?: Record<string, unknown> | null;
  created_at?: string;
  sequence?: number;
  traces?: ToolTrace[];
  skills?: SkillNotice[];
  pending?: boolean;
  error?: string;
  reasoningSummary?: string;
  reasoningEffort?: ReasoningEffort;
}

export interface PublicConfig {
  model_id: string;
  summary_model_id?: string | null;
  summary_uses_main?: boolean;
  limits?: {
    max_turns?: number;
    token_budget?: number;
  };
  features?: {
    skills?: boolean;
    abort?: boolean;
  };
}

export interface ProviderConfigView {
  base_url: string;
  api_key: string;
  model: string;
}

export interface UserLLMConfig {
  main: ProviderConfigView;
  summary: ProviderConfigView;
  has_config: boolean;
}

export interface UserLLMConfigInput {
  main: ProviderConfigView;
  summary: ProviderConfigView;
}

export type AgentEvent =
  | { type: "text_delta"; delta: string }
  | { type: "reasoning_delta"; delta: string }
  | { type: "tool_call"; call_id: string; name: string; arguments?: unknown }
  | {
      type: "tool_result";
      call_id?: string;
      tool_call_id?: string;
      name?: string;
      result?: unknown;
      error?: boolean;
      truncated?: boolean;
      patch?: string;
      patch_truncated?: boolean;
    }
  | { type: "skill_loaded"; name: string; already_loaded?: boolean }
  | { type: "error"; message: string; code?: string; recoverable?: boolean }
  | { type: "done"; session_id?: string; finish_reason?: string; message_id?: string }
  | { type: string; [key: string]: unknown };
