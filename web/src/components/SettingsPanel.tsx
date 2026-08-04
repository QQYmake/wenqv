import { FormEvent, useEffect, useState } from "react";
import type { AgentApi } from "../api/client";
import type { ProviderConfigView, UserLLMConfigInput } from "../types";
import { Icon } from "./Icon";

interface SettingsPanelProps {
  client: AgentApi;
  onClose: () => void;
}

const EMPTY: ProviderConfigView = { base_url: "", api_key: "", model: "" };

function makeFields(initial?: UserLLMConfigInput | null): UserLLMConfigInput {
  return {
    main: { ...(initial?.main ?? EMPTY) },
    summary: { ...(initial?.summary ?? EMPTY) },
  };
}

function makeEditableFields(initial?: UserLLMConfigInput | null): UserLLMConfigInput {
  // The editable form starts with a blank api_key (the masked value is only a
  // hint); leaving it blank keeps the stored key server-side.
  const fields = makeFields(initial);
  return {
    main: { ...fields.main, api_key: "" },
    summary: { ...fields.summary, api_key: "" },
  };
}

export function SettingsPanel({ client, onClose }: SettingsPanelProps) {
  const [fields, setFields] = useState<UserLLMConfigInput>(() => makeEditableFields(null));
  const [masked, setMasked] = useState<UserLLMConfigInput>(() => makeFields(null));
  const [hasConfig, setHasConfig] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    client
      .getUserConfig()
      .then((config) => {
        if (!alive) return;
        const input = makeEditableFields(config);
        setFields(input);
        setMasked(makeFields(config));
        setHasConfig(Boolean(config.has_config));
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "无法读取配置");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [client]);

  const update = (role: "main" | "summary", key: keyof ProviderConfigView, value: string) => {
    setFields((current) => ({
      ...current,
      [role]: { ...current[role], [key]: value },
    }));
    setTestResult(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const saved = await client.putUserConfig(fields);
      setMasked(makeFields(saved));
      setHasConfig(Boolean(saved.has_config));
      // Keep api_key field empty after save so the masked value is not edited.
      setFields((current) => ({
        ...current,
        main: { ...current.main, api_key: "" },
        summary: { ...current.summary, api_key: "" },
      }));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await client.testUserConfig(fields);
      setTestResult(result);
    } catch (err: unknown) {
      setTestResult({ ok: false, detail: err instanceof Error ? err.message : "请求失败" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="settings-modal" role="dialog" aria-label="API 配置">
      <div className="settings-modal__card">
        <header className="settings-modal__header">
          <div>
            <span className="eyebrow">SETTINGS</span>
            <h2>API 配置</h2>
          </div>
          <button type="button" className="settings-close" onClick={onClose} aria-label="关闭设置">
            <Icon name="close" />
          </button>
        </header>

        <p className="settings-intro">
          你的 API key 仅在本工作区加密保存，不会回传明文。留空字段将回退到服务器默认配置。
          {hasConfig ? "（已配置）" : "（尚未配置，请填写后再对话）"}
        </p>

        <form className="settings-form" onSubmit={submit}>
          <fieldset className="settings-fieldset">
            <legend>主模型 (main)</legend>
            <label>
              <span>Base URL</span>
              <input
                type="url"
                value={fields.main.base_url}
                onChange={(e) => update("main", "base_url", e.target.value)}
                placeholder="https://api.openai.com/v1"
              />
            </label>
            <label>
              <span>API Key {masked.main.api_key ? `（已保存：${masked.main.api_key}）` : ""}</span>
              <input
                type="password"
                value={fields.main.api_key}
                onChange={(e) => update("main", "api_key", e.target.value)}
                placeholder={masked.main.api_key || "sk-..."}
                autoComplete="off"
              />
            </label>
            <label>
              <span>Model</span>
              <input
                type="text"
                value={fields.main.model}
                onChange={(e) => update("main", "model", e.target.value)}
                placeholder="gpt-4o-mini"
              />
            </label>
          </fieldset>

          <fieldset className="settings-fieldset">
            <legend>摘要模型 (summary, 可选)</legend>
            <label>
              <span>Base URL</span>
              <input
                type="url"
                value={fields.summary.base_url}
                onChange={(e) => update("summary", "base_url", e.target.value)}
                placeholder="留空则复用主模型"
              />
            </label>
            <label>
              <span>API Key {masked.summary.api_key ? `（已保存：${masked.summary.api_key}）` : ""}</span>
              <input
                type="password"
                value={fields.summary.api_key}
                onChange={(e) => update("summary", "api_key", e.target.value)}
                placeholder={masked.summary.api_key || "留空则复用主模型"}
                autoComplete="off"
              />
            </label>
            <label>
              <span>Model</span>
              <input
                type="text"
                value={fields.summary.model}
                onChange={(e) => update("summary", "model", e.target.value)}
                placeholder="留空则复用主模型"
              />
            </label>
          </fieldset>

          {error && <p className="settings-error" role="alert">{error}</p>}
          {testResult && (
            <p className={testResult.ok ? "settings-test settings-test--ok" : "settings-test settings-test--fail"}>
              {testResult.ok ? "连接成功" : `连接失败：${testResult.detail}`}
            </p>
          )}

          <div className="settings-actions">
            <button type="button" className="settings-test-button" onClick={() => void testConnection()} disabled={testing}>
              {testing ? "测试中…" : "测试连接"}
            </button>
            <button type="submit" className="settings-save-button" disabled={saving}>
              {saving ? "保存中…" : "保存配置"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}