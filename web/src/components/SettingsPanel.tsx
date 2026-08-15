import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import type { AgentApi } from "../api/client";
import { blankProviderConfig, loadProviderConfig, LocalPrivacyError, saveProviderConfig } from "../storage/local";
import type { ModelRole, ProviderConfigSet, ProviderConfigView } from "../types";
import { Icon } from "./Icon";

interface SettingsPanelProps {
  client: AgentApi;
  workspaceId: string;
  onClose: () => void;
  onConfigSaved?: (modelId: string) => void;
}

const EMPTY: ProviderConfigView = { base_url: "", api_key: "", model: "" };

function editable(config: ProviderConfigSet): ProviderConfigSet {
  return {
    main: { ...config.main, api_key: "" },
    summary: { ...config.summary, api_key: "" },
  };
}

function merge(saved: ProviderConfigSet, draft: ProviderConfigSet): ProviderConfigSet {
  const role = (name: ModelRole): ProviderConfigView => ({
    ...saved[name],
    ...draft[name],
    base_url: draft[name].base_url.trim(),
    model: draft[name].model.trim(),
    api_key: draft[name].api_key || saved[name].api_key,
  });
  return { main: role("main"), summary: role("summary") };
}

function complete(provider: ProviderConfigView): boolean {
  return Boolean(provider.base_url && provider.api_key && provider.model);
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

function ModelCombobox({ label, models, onChange, onSelect, placeholder, role, value }: ModelComboboxProps) {
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listboxId = `settings-model-options-${role}`;
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);
  const select = (model: string) => {
    onSelect(model);
    setOpen(false);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") return setOpen(false);
    if (!models.length || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Enter" && open && highlightedIndex >= 0) return select(models[highlightedIndex]);
    setOpen(true);
    setHighlightedIndex((index) => event.key === "ArrowUp" ? (index <= 0 ? models.length - 1 : index - 1) : (index + 1) % models.length);
  };
  return <div className="settings-model-combobox" ref={wrapperRef}>
    <input aria-autocomplete="list" aria-controls={listboxId} aria-expanded={open} aria-haspopup="listbox"
      aria-label={`${label} Model`} role="combobox" type="text" value={value}
      onChange={(event) => { onChange(event.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
      onKeyDown={onKeyDown} placeholder={placeholder} />
    {open && <div className="settings-model-dropdown" id={listboxId} role="listbox" aria-label={`${label} 模型候选`}>
      {models.length ? models.map((model, index) => <button type="button"
        className={`settings-model-option${index === highlightedIndex ? " settings-model-option--highlighted" : ""}`}
        key={model} role="option" aria-selected={model === value} onMouseEnter={() => setHighlightedIndex(index)} onClick={() => select(model)}>{model}</button>)
        : <button type="button" className="settings-model-option settings-model-option--empty" role="option" aria-disabled="true" disabled>未拉取到模型</button>}
    </div>}
  </div>;
}

export function SettingsPanel({ client, workspaceId, onClose, onConfigSaved }: SettingsPanelProps) {
  const savedRef = useRef<ProviderConfigSet>(blankProviderConfig());
  const [fields, setFields] = useState<ProviderConfigSet>(blankProviderConfig);
  const [hasConfig, setHasConfig] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [models, setModels] = useState<Record<ModelRole, string[]>>({ main: [], summary: [] });
  const [modelLoading, setModelLoading] = useState<Record<ModelRole, boolean>>({ main: false, summary: false });

  useEffect(() => {
    let alive = true;
    void loadProviderConfig(workspaceId).then((config) => {
      if (!alive) return;
      const current = config ?? blankProviderConfig();
      savedRef.current = current;
      setFields(editable(current));
      setHasConfig(complete(current.main));
    }).catch((cause: unknown) => {
      if (!alive) return;
      setFields(blankProviderConfig());
      setHasConfig(false);
      setError(cause instanceof LocalPrivacyError && cause.code === "provider_reconfigure"
        ? "本机加密密钥或配置已不可用，请重新填写 API 配置。" : "无法读取本地加密配置。");
    }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [workspaceId]);

  const update = (role: ModelRole, key: keyof ProviderConfigView, value: string) => {
    setFields((current) => ({ ...current, [role]: { ...current[role], [key]: value } }));
    if (key === "base_url" || key === "api_key") setModels((current) => ({ ...current, [role]: [] }));
    setTestResult(null);
  };
  const resolved = () => merge(savedRef.current, fields);
  const fetchModels = async (role: ModelRole) => {
    const provider = resolved()[role];
    if (!provider.base_url || !provider.api_key) return setModels((current) => ({ ...current, [role]: [] }));
    setModelLoading((current) => ({ ...current, [role]: true }));
    try {
      const result = await client.listModels({ base_url: provider.base_url, api_key: provider.api_key });
      setModels((current) => ({ ...current, [role]: Array.isArray(result.models) ? result.models : [] }));
    } catch {
      setModels((current) => ({ ...current, [role]: [] }));
    } finally {
      setModelLoading((current) => ({ ...current, [role]: false }));
    }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const next = resolved();
    if (!complete(next.main)) return setError("请填写完整的主模型 Base URL、API Key 和 Model。");
    if ([next.summary.base_url, next.summary.api_key, next.summary.model].some(Boolean) && !complete(next.summary)) {
      return setError("摘要模型请完整填写，或全部留空以复用主模型。");
    }
    setSaving(true); setError(null);
    try {
      await saveProviderConfig(workspaceId, next);
      savedRef.current = next;
      setFields(editable(next));
      setHasConfig(true);
      onConfigSaved?.(next.main.model);
    } catch {
      setError("无法加密保存配置；请确认此浏览器支持 Web Crypto 与 IndexedDB。");
    } finally { setSaving(false); }
  };
  const testConnection = async () => {
    const next = resolved();
    if (!complete(next.main)) return setError("请先填写完整的主模型配置。");
    setTesting(true); setError(null); setTestResult(null);
    try { setTestResult((await client.testProvider(next)).ok); } catch { setTestResult(false); } finally { setTesting(false); }
  };
  const fieldset = (role: ModelRole, title: string, optional = false) => <fieldset className={`settings-fieldset settings-fieldset--${role}`}>
    <legend>{title}</legend>
    <label><span>Base URL</span><input type="url" value={fields[role].base_url} onChange={(event) => update(role, "base_url", event.target.value)} placeholder={optional ? "留空则复用主模型" : "https://api.openai.com/v1"} /></label>
    <label><span>API Key {savedRef.current[role].api_key ? "（已在本浏览器保存）" : ""}</span><input type="password" value={fields[role].api_key} onChange={(event) => update(role, "api_key", event.target.value)} placeholder={savedRef.current[role].api_key ? "留空则保留当前 Key" : "sk-..."} autoComplete="off" /></label>
    <div className="settings-model-field"><div className="settings-model-label-row"><span>Model</span><button type="button" className="settings-fetch-models-button" onClick={() => void fetchModels(role)} disabled={modelLoading[role]}>{modelLoading[role] ? "拉取中…" : "拉取"}</button></div>
      <ModelCombobox label={role === "main" ? "主模型" : "摘要模型"} models={models[role]} onChange={(value) => update(role, "model", value)} onSelect={(value) => update(role, "model", value)} placeholder={optional ? "留空则复用主模型" : "gpt-4o-mini"} role={role} value={fields[role].model} />
    </div>
  </fieldset>;
  return <div className="settings-modal" role="dialog" aria-label="API 配置" aria-modal="true" aria-describedby="settings-modal-description">
    <div className="settings-modal__card"><div className="settings-modal__surface"><header className="settings-modal__header"><div className="settings-modal__heading"><div><span className="eyebrow">SETTINGS</span><h2>API 配置</h2></div><span className={`settings-config-status settings-config-status--${loading ? "loading" : hasConfig ? "configured" : "unconfigured"}`} role="status" aria-busy={loading}>{loading ? "读取中…" : hasConfig ? "已配置" : "尚未配置"}</span></div><button type="button" className="settings-close" onClick={onClose} aria-label="关闭设置"><Icon name="close" /></button></header>
      <p className="settings-modal__description" id="settings-modal-description">API Key 仅加密保存在当前浏览器的 IndexedDB 中。发送模型请求时才会临时解密；服务器不会保存配置或 Key。</p>
      <form className="settings-form" onSubmit={submit}>{fieldset("main", "主模型 (main)")}{fieldset("summary", "摘要模型 (summary, 可选)", true)}
        {(error || testResult !== null) && <div className="settings-feedback" aria-live={error ? "assertive" : "polite"}>{error && <p className="settings-error" role="alert">{error}</p>}{testResult !== null && <p className={testResult ? "settings-test settings-test--ok" : "settings-test settings-test--fail"}>{testResult ? "连接成功" : "连接失败：请检查 Provider 配置。"}</p>}</div>}
        <div className="settings-actions"><button type="button" className="settings-test-button" onClick={() => void testConnection()} disabled={testing}>{testing ? "测试中…" : "测试连接"}</button><button type="submit" className="settings-save-button" disabled={saving}>{saving ? "保存中…" : "保存配置"}</button></div>
      </form>
    </div></div>
  </div>;
}
