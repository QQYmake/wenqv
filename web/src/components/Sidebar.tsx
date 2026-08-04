import { FormEvent, useEffect, useRef, useState } from "react";
import type { Session, Theme } from "../types";
import { Icon } from "./Icon";

interface SidebarProps {
  sessions: Session[];
  activeSessionId: string | null;
  expanded: boolean;
  loading: boolean;
  theme: Theme;
  onExpand: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onThemeChange: (theme: Theme) => void;
}

function titleInitial(title: string) {
  return title.trim().slice(0, 1) || "·";
}

export function Sidebar({
  sessions,
  activeSessionId,
  expanded,
  loading,
  theme,
  onExpand,
  onNew,
  onSelect,
  onRename,
  onDelete,
  onThemeChange,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId) inputRef.current?.select();
  }, [editingId]);

  const startRename = (session: Session) => {
    setEditingId(session.id);
    setEditingTitle(session.title);
  };

  const finishRename = async (event?: FormEvent) => {
    event?.preventDefault();
    const id = editingId;
    const title = editingTitle.trim();
    setEditingId(null);
    if (id && title) await onRename(id, title);
  };

  const askDelete = async (session: Session) => {
    const confirmed = window.confirm(`删除“${session.title}”？此操作无法撤销。`);
    if (confirmed) await onDelete(session.id);
  };

  return (
    <aside
      className={`sidebar${expanded ? " sidebar--expanded" : ""}`}
      aria-label="对话侧栏"
      onClick={(event) => {
        if (!expanded && !(event.target as Element).closest("button, input")) onExpand();
      }}
    >
      {!expanded && (
        <button className="sidebar-expand-zone" onClick={onExpand} aria-label="展开侧栏">
          <span className="sr-only">展开侧栏</span>
        </button>
      )}

      <div className="sidebar-logo" aria-hidden="true">
        <span className="logo-sun" />
        <span className="logo-wave logo-wave--one" />
        <span className="logo-wave logo-wave--two" />
      </div>

      <nav className="sidebar-primary" aria-label="主要操作">
        <button className="rail-action new-chat-action" onClick={onNew} title="新对话">
          <span className="rail-icon"><Icon name="add" /></span>
          {expanded && <span>新对话</span>}
        </button>
        <div className="rail-section-label" aria-hidden={!expanded}>
          <Icon name="history" />
          {expanded && <span>最近</span>}
        </div>
      </nav>

      <div className="session-list" aria-label="历史对话">
        {loading && expanded && <p className="sidebar-muted">水面正在展开…</p>}
        {!loading && sessions.length === 0 && expanded && (
          <p className="sidebar-muted">还没有对话</p>
        )}
        {sessions.map((session) => {
          const active = session.id === activeSessionId;
          return (
            <div
              className={`session-row${active ? " session-row--active" : ""}`}
              key={session.id}
            >
              {editingId === session.id && expanded ? (
                <form className="session-rename-form" onSubmit={finishRename}>
                  <input
                    ref={inputRef}
                    value={editingTitle}
                    onChange={(event) => setEditingTitle(event.target.value)}
                    onBlur={() => void finishRename()}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") setEditingId(null);
                    }}
                    aria-label="对话名称"
                  />
                </form>
              ) : (
                <button
                  className="session-select"
                  onClick={() => onSelect(session.id)}
                  title={session.title}
                  aria-current={active ? "page" : undefined}
                >
                  {!expanded && <span className="session-initial">{titleInitial(session.title)}</span>}
                  {expanded && <span className="session-title">{session.title}</span>}
                </button>
              )}

              {expanded && editingId !== session.id && (
                <div className="session-actions">
                  <button onClick={() => startRename(session)} aria-label={`重命名 ${session.title}`}>
                    <Icon name="rename" />
                  </button>
                  <button onClick={() => void askDelete(session)} aria-label={`删除 ${session.title}`}>
                    <Icon name="trash" />
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="sidebar-footer">
        <button
          className="rail-action theme-toggle"
          onClick={() => onThemeChange(theme === "light" ? "dark" : "light")}
          title={theme === "light" ? "切换为深色" : "切换为浅色"}
          aria-label={theme === "light" ? "切换为深色模式" : "切换为浅色模式"}
        >
          <span className="rail-icon"><Icon name={theme === "light" ? "moon" : "sun"} /></span>
          {expanded && <span>{theme === "light" ? "入夜" : "天明"}</span>}
        </button>
      </div>
    </aside>
  );
}
