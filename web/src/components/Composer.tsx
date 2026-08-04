import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import type { Skill } from "../types";
import { Icon } from "./Icon";

interface ComposerProps {
  value: string;
  modelId: string;
  skills: Skill[];
  selectedSkills: Set<string>;
  streaming: boolean;
  abortEnabled?: boolean;
  apiConfigured?: boolean;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onAbort: () => void;
  onToggleSkill: (name: string, selected: boolean) => void;
  onOpenSettings?: () => void;
}

function currentMention(value: string) {
  return value.match(/(?:^|\s)@([\w-]*)$/u);
}

export function Composer({
  value,
  modelId,
  skills,
  selectedSkills,
  streaming,
  abortEnabled = true,
  apiConfigured = true,
  onChange,
  onSubmit,
  onAbort,
  onToggleSkill,
  onOpenSettings,
}: ComposerProps) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const settingsRef = useRef<HTMLDivElement>(null);
  const mention = currentMention(value);
  const mentionQuery = mention?.[1].toLocaleLowerCase() ?? "";
  const mentionMatches = useMemo(
    () =>
      mention
        ? skills
            .filter(
              (skill) =>
                skill.name.toLocaleLowerCase().includes(mentionQuery) ||
                skill.description.toLocaleLowerCase().includes(mentionQuery),
            )
            .slice(0, 6)
        : [],
    [mention, mentionQuery, skills],
  );
  const canSend = value.trim().length > 0 && !streaming && apiConfigured;

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 176)}px`;
  }, [value]);

  useEffect(() => {
    if (!settingsOpen) return;
    const close = (event: PointerEvent) => {
      if (!settingsRef.current?.contains(event.target as Node)) setSettingsOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [settingsOpen]);

  useEffect(() => setMentionIndex(0), [mentionQuery]);

  const chooseMention = (skill: Skill) => {
    if (!mention) return;
    const start = (mention.index ?? 0) + (mention[0].startsWith(" ") ? 1 : 0);
    const next = `${value.slice(0, start)}@${skill.name} `;
    onChange(next);
    onToggleSkill(skill.name, true);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionMatches.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setMentionIndex((index) => (index + 1) % mentionMatches.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setMentionIndex((index) => (index - 1 + mentionMatches.length) % mentionMatches.length);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseMention(mentionMatches[mentionIndex]);
        return;
      }
      if (event.key === "Escape") {
        onChange(`${value} `);
        return;
      }
    }

    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (canSend) onSubmit(value);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (canSend) onSubmit(value);
  };

  return (
    <div className="composer-wrap">
      <form className="composer" onSubmit={submit} aria-label="发送消息">
        {selectedSkills.size > 0 && (
          <div className="selected-skills" aria-label="本轮已选 Skills">
            {[...selectedSkills].map((name) => (
              <button
                type="button"
                key={name}
                className="skill-chip"
                onClick={() => onToggleSkill(name, false)}
                aria-label={`移除 Skill ${name}`}
              >
                <Icon name="leaf" />
                <span>{name}</span>
                <Icon name="close" />
              </button>
            ))}
          </div>
        )}

        {mentionMatches.length > 0 && (
          <div className="mention-menu" role="listbox" aria-label="Skill 建议">
            <div className="mention-caption">注入 Skill</div>
            {mentionMatches.map((skill, index) => (
              <button
                type="button"
                role="option"
                aria-selected={mentionIndex === index}
                className={mentionIndex === index ? "mention-option mention-option--active" : "mention-option"}
                key={skill.name}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseMention(skill)}
              >
                <span className="mention-name">@{skill.name}</span>
                <span className="mention-description">{skill.description}</span>
              </button>
            ))}
          </div>
        )}

        <textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={apiConfigured ? "请输入···" : "请先配置 API"}
          aria-label="消息"
          rows={1}
          disabled={!apiConfigured}
        />

        {!apiConfigured && (
          <button type="button" className="composer-config-hint" onClick={onOpenSettings}>
            前往设置 API →
          </button>
        )}

        <div className="composer-footer">
          <div className="composer-meta">
            <span className="model-tag" title={`当前模型：${modelId}`}>
              {modelId || "读取模型…"}
            </span>
          </div>

          <div className="composer-actions" ref={settingsRef}>
            {streaming && abortEnabled && (
              <button type="button" className="abort-button" onClick={onAbort} aria-label="停止生成">
                <Icon name="stop" />
                <span>停止</span>
              </button>
            )}

            <button
              type="button"
              className={`chat-settings-button${settingsOpen ? " chat-settings-button--active" : ""}`}
              onClick={() => setSettingsOpen((open) => !open)}
              aria-expanded={settingsOpen}
              aria-haspopup="dialog"
              aria-label="聊天设置"
            >
              <Icon name="settings" />
              {selectedSkills.size > 0 && <span className="settings-count">{selectedSkills.size}</span>}
            </button>

            <button
              type="submit"
              className={`send-button${canSend ? " send-button--ready" : ""}`}
              disabled={!canSend}
              aria-label={canSend ? "发送消息" : "请输入消息后发送"}
            >
              <Icon name={canSend ? "arrow-up" : "arrow-down"} />
            </button>

            {settingsOpen && (
              <div className="settings-popover" role="dialog" aria-label="聊天设置">
                <div className="settings-popover__inner">
                  <div className="settings-heading">
                    <div>
                      <span className="eyebrow">CONTEXT</span>
                      <h3>Skills</h3>
                    </div>
                    <span>{selectedSkills.size} 已选</span>
                  </div>
                  <p className="settings-intro">选择后将在下一轮载入本会话；也可直接输入 @。</p>
                  <div className="skill-options">
                    {skills.length === 0 && <p className="empty-skills">暂无可用 Skill</p>}
                    {skills.map((skill) => {
                      const checked = selectedSkills.has(skill.name);
                      return (
                        <label className="skill-option" key={skill.name}>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) => onToggleSkill(skill.name, event.target.checked)}
                          />
                          <span className="custom-checkbox" aria-hidden="true">
                            {checked && <Icon name="check" />}
                          </span>
                          <span>
                            <strong>{skill.name}</strong>
                            <small>{skill.description}</small>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </form>
      <p className="composer-note">Agent 可能出错，请核对重要信息</p>
    </div>
  );
}
