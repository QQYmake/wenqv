import { useEffect, useRef } from "react";
import type { ChatMessage, SkillNotice } from "../types";
import { ExecutionTrace } from "./ExecutionTrace";
import { Icon } from "./Icon";
import { Markdown } from "./Markdown";

function SkillStrip({ notice }: { notice: SkillNotice }) {
  return (
    <div className="skill-strip" role="status">
      <Icon name="leaf" />
      <span>
        <strong>{notice.name}</strong>
        {notice.removed
          ? " 已从上下文移除"
          : notice.alreadyLoaded
            ? " 已在本会话中"
            : " 已载入上下文"}
      </span>
    </div>
  );
}

function PersistedSystemMessage({ message }: { message: ChatMessage }) {
  const skillName =
    message.name ??
    (typeof message.metadata?.skill_name === "string" ? message.metadata.skill_name : undefined);
  const isSkill = message.kind?.includes("skill") || Boolean(skillName);

  if (isSkill) {
    return (
      <SkillStrip
        notice={{
          name: skillName ?? "Skill",
          alreadyLoaded: !message.kind?.includes("removed"),
          removed: message.kind?.includes("removed"),
        }}
      />
    );
  }
  if (message.kind?.includes("summary")) {
    return (
      <div className="context-strip" role="status">
        较早的对话已压缩为摘要
      </div>
    );
  }
  return null;
}

function AssistantMark({ pending }: { pending?: boolean }) {
  return (
    <div className={`assistant-mark${pending ? " assistant-mark--thinking" : ""}`} aria-hidden="true">
      <span />
    </div>
  );
}

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const shouldFollowRef = useRef(true);

  useEffect(() => {
    if (!shouldFollowRef.current) return;
    const viewport = viewportRef.current;
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [messages]);

  return (
    <div
      className="message-viewport"
      ref={viewportRef}
      onScroll={(event) => {
        const element = event.currentTarget;
        shouldFollowRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
      }}
    >
      <div className="message-list" aria-live="polite" aria-busy={messages.some((message) => message.pending)}>
        {messages.map((message) => {
          const kind = message.kind?.toLowerCase() ?? "";
          if (
            message.role === "system" ||
            kind.includes("skill") ||
            kind.includes("summary")
          ) {
            return <PersistedSystemMessage key={message.id} message={message} />;
          }
          if (message.role === "tool") return null;

          if (message.role === "user") {
            return (
              <article className="message message--user" key={message.id}>
                <div className="user-bubble">{message.content}</div>
              </article>
            );
          }

          return (
            <article className="message message--assistant" key={message.id}>
              <AssistantMark pending={message.pending && !message.content} />
              <div className="assistant-content">
                {(message.skills ?? []).map((notice) => (
                  <SkillStrip key={`${message.id}-${notice.name}`} notice={notice} />
                ))}
                {(message.traces?.length ?? 0) > 0 && <ExecutionTrace traces={message.traces ?? []} />}
                {message.content ? (
                  <Markdown>{message.content}</Markdown>
                ) : (
                  message.pending && (
                    <div className="thinking-line" role="status">
                      <span />
                      <span />
                      <span />
                      <span className="sr-only">正在思考</span>
                    </div>
                  )
                )}
                {message.error && <div className="message-error">{message.error}</div>}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
