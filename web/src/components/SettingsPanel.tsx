import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import type { AgentApi } from "../api/client";
import type { ModelRole, ProviderConfigView, UserLLMConfigInput } from "../types";
import { Icon } from "./Icon";

interface SettingsPanelProps {
  client: AgentApi;
  onClose: () => void;
  /** Called after a successful save so the app can refresh its config state. */
  onConfigSaved?: () => void;
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

interface ModelComboboxProps {
  label: string;
  models: string[];
  onChange: (value: string) => void;
  onSelect: (value: string) => void;
  placeholder: string;
  role: ModelRole;
  value: string;
}

function ModelCombobox({
  label,
  models,
  onChange,
  onSelect,
  placeholder,
  role,
  value,
}: ModelComboboxProps) {
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listboxId = `settings-model-options-${role}`;

  useEffect(() => {
    const handleDocumentPointerDown = (event: PointerEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", handleDocumentPointerDown);
    return () => document.removeEventListener("pointerdown", handleDocumentPointerDown);
  }, []);

  useEffect(() => {
    setHighlightedIndex(open && models.length > 0 ? 0 : -1);
  }, [models, open]);

  const select = (model: string) => {
    onSelect(model);
    setOpen(false);
    setHighlightedIndex(-1);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      if (models.length > 0) {
        setHighlightedIndex((current) => (current < models.length - 1 ? current + 1 : 0));
      }
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      if (models.length > 0) {
        setHighlightedIndex((current) => (current > 0 ? current - 1 : models.length - 1));
      }
      return;
    }
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (event.key === "Enter" && open && highlightedIndex >= 0) {
      event.preventDefault();
      select(models[highlightedIndex]);
    }
  };

  return (
    <div className="settings-model-combobox" ref={wrapperRef}>
      <input
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={`${label} Model`}
        role="combobox"
        type="text"
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
      />
      {open && (
        <div className="settings-model-dropdown" id={listboxId} role="listbox" aria-label={`${label} 模型候选`}>
          {models.length > 0 ? (
            models.map((model, index) => (
              <button
                type="button"
                className={`settings-model-option${index === highlightedIndex ? " settings-model-option--highlighted" : ""}`}
                key={model}
                role="option"
                aria-selected={model === value}
                onMouseEnter={() => setHighlightedIndex(index)}
                onClick={() => select(model)}
              >
                {model}
              </button>
            ))
          ) : (
            <button
              type="button"
              className="settings-model-option settings-model-option--empty"
              role="option"
              aria-disabled="true"
              disabled
            >
              未拉取到模型
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function SettingsPanel({ client, onClose, onConfigSaved }: SettingsPanelProps) {
  const [fields, setFields] = useState<UserLLMConfigInput>(() => makeEditableFields(null));
  const [masked, setMasked] = useState<UserLLMConfigInput>(() => makeFields(null));
  const [hasConfig, setHasConfig] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [models, setModels] = useState<Record<ModelRole, string[]>>({ main: [], summary: [] });
  const [modelLoading, setModelLoading] = useState<Record<ModelRole, boolean>>({ main: false, summary: false });
  const modelProviderVersions = useRef<Record<ModelRole, number>>({ main: 0, summary: 0 });
  const modelRequestIds = useRef<Record<ModelRole, number>>({ main: 0, summary: 0 });

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

  const update = (role: ModelRole, key: keyof ProviderConfigView, value: string) => {
    setFields((current) => ({
      ...current,
      [role]: { ...current[role], [key]: value },
    }));
    if (key === "base_url" || key === "api_key") {
      modelProviderVersions.current[role] += 1;
      setModels((current) => ({ ...current, [role]: [] }));
    }
    setTestResult(null);
  };

  const fetchModels = async (role: ModelRole) => {
    const requestId = modelRequestIds.current[role] + 1;
    const providerVersion = modelProviderVersions.current[role];
    modelRequestIds.current[role] = requestId;
    setModelLoading((current) => ({ ...current, [role]: true }));
    try {
      const provider = fields[role];
      const result = await client.listModels({
        role,
        base_url: provider.base_url,
        api_key: provider.api_key,
      });
      if (
        modelRequestIds.current[role] === requestId &&
        modelProviderVersions.current[role] === providerVersion
      ) {
        setModels((current) => ({ ...current, [role]: Array.isArray(result.models) ? result.models : [] }));
      }
    } catch {
      if (
        modelRequestIds.current[role] === requestId &&
        modelProviderVersions.current[role] === providerVersion
      ) {
        setModels((current) => ({ ...current, [role]: [] }));
      }
    } finally {
      if (modelRequestIds.current[role] === requestId) {
        setModelLoading((current) => ({ ...current, [role]: false }));
      }
    }
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
      onConfigSaved?.();
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
    <div
      className="settings-modal"
      role="dialog"
      aria-label="API 配置"
      aria-modal="true"
      aria-describedby="settings-modal-description"
    >
      <div className="settings-modal__card">
        <div className="settings-modal__surface">
          <header className="settings-modal__header">
            <div className="settings-modal__heading">
              <div>
                <span className="eyebrow">SETTINGS</span>
                <h2>API 配置</h2>
              </div>
              <span
                className={`settings-config-status settings-config-status--${
                  loading ? "loading" : hasConfig ? "configured" : "unconfigured"
                }`}
                role="status"
                aria-busy={loading}
              >
                {loading ? "读取中…" : hasConfig ? "已配置" : "尚未配置"}
              </span>
            </div>
            <button type="button" className="settings-close" onClick={onClose} aria-label="关闭设置">
              <Icon name="close" />
            </button>
          </header>

          <p className="settings-modal__description" id="settings-modal-description">
            你的 API key 仅在本工作区加密保存，不会回传明文。留空字段将回退到服务器默认配置。
            {!loading && !hasConfig && "尚未配置，请填写后再对话。"}
          </p>

          <form className="settings-form" onSubmit={submit}>
            <fieldset className="settings-fieldset settings-fieldset--main">
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
              <div className="settings-model-field">
                <div className="settings-model-label-row">
                  <span>Model</span>
                  <button
                    type="button"
                    className="settings-fetch-models-button"
                    onClick={() => void fetchModels("main")}
                    disabled={modelLoading.main}
                  >
                    {modelLoading.main ? "拉取中…" : "拉取"}
                  </button>
                </div>
                <ModelCombobox
                  label="主模型"
                  models={models.main}
                  onChange={(value) => update("main", "model", value)}
                  onSelect={(value) => update("main", "model", value)}
                  placeholder="gpt-4o-mini"
                  role="main"
                  value={fields.main.model}
                />
              </div>
            </fieldset>

            <fieldset className="settings-fieldset settings-fieldset--summary">
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
              <div className="settings-model-field">
                <div className="settings-model-label-row">
                  <span>Model</span>
                  <button
                    type="button"
                    className="settings-fetch-models-button"
                    onClick={() => void fetchModels("summary")}
                    disabled={modelLoading.summary}
                  >
                    {modelLoading.summary ? "拉取中…" : "拉取"}
                  </button>
                </div>
                <ModelCombobox
                  label="摘要模型"
                  models={models.summary}
                  onChange={(value) => update("summary", "model", value)}
                  onSelect={(value) => update("summary", "model", value)}
                  placeholder="留空则复用主模型"
                  role="summary"
                  value={fields.summary.model}
                />
              </div>
            </fieldset>

            {(error || testResult) && (
              <div className="settings-feedback" aria-live={error ? "assertive" : "polite"}>
                {error && <p className="settings-error" role="alert">{error}</p>}
                {testResult && (
                  <p className={testResult.ok ? "settings-test settings-test--ok" : "settings-test settings-test--fail"}>
                    {testResult.ok ? "连接成功" : `连接失败：${testResult.detail}`}
                  </p>
                )}
              </div>
            )}

            <div className="settings-actions">
              <button
                type="button"
                className="settings-test-button"
                onClick={() => void testConnection()}
                disabled={testing}
              >
                {testing ? "测试中…" : "测试连接"}
              </button>
              <button type="submit" className="settings-save-button" disabled={saving}>
                {saving ? "保存中…" : "保存配置"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
