"""Token-budgeted context preparation and lightweight summary tasks."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .models import ChatMessage
from .ports import LLMClientProvider


TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """Conservative dependency-free token estimate for mixed Chinese/English.

    Production wiring may inject a tokenizer matching the selected model.
    """

    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\u3040" <= char <= "\u30ff"
    )
    non_cjk = max(0, len(text) - cjk)
    return cjk + math.ceil(non_cjk / 4)


@dataclass(frozen=True, slots=True)
class ContextConfig:
    token_budget: int = 32_000
    summary_trigger_ratio: float = 0.8
    min_recent_messages: int = 6
    summary_max_tokens: int = 1_024
    title_max_tokens: int = 32

    def __post_init__(self) -> None:
        if self.token_budget < 128:
            raise ValueError("token_budget must be at least 128")
        if not 0 < self.summary_trigger_ratio <= 1:
            raise ValueError("summary_trigger_ratio must be in (0, 1]")
        if self.min_recent_messages < 1:
            raise ValueError("min_recent_messages must be positive")


@dataclass(frozen=True, slots=True)
class ContextPreparation:
    messages: tuple[ChatMessage, ...]
    token_count_before: int
    token_count_after: int
    changed: bool = False
    summarized: bool = False
    summary_failed: bool = False
    dropped_messages: int = 0


class ContextManager:
    def __init__(
        self,
        clients: LLMClientProvider,
        config: ContextConfig = ContextConfig(),
        *,
        token_counter: TokenCounter = estimate_tokens,
    ) -> None:
        self.clients = clients
        self.config = config
        self.token_counter = token_counter

    def count_messages(self, messages: Sequence[ChatMessage]) -> int:
        total = 0
        for message in messages:
            total += 4
            total += self.token_counter(message.content or "")
            total += self.token_counter(message.name or "")
            for call in message.tool_calls:
                import json

                total += self.token_counter(call.name)
                total += self.token_counter(
                    json.dumps(dict(call.arguments), ensure_ascii=False, default=str)
                )
        return total

    async def prepare(self, messages: Sequence[ChatMessage]) -> ContextPreparation:
        original = tuple(messages)
        before = self.count_messages(original)
        trigger = int(self.config.token_budget * self.config.summary_trigger_ratio)
        if before <= trigger:
            return ContextPreparation(original, before, before)

        groups = _message_groups(original)
        recent_start = _recent_group_start(groups, self.config.min_recent_messages)
        early_groups = groups[:recent_start]
        recent_groups = groups[recent_start:]
        protected_groups = [group for group in early_groups if _is_protected_group(group)]
        summarizable_groups = [
            group for group in early_groups if not _is_protected_group(group)
        ]

        summary_failed = False
        summarized = False
        summary_message: ChatMessage | None = None
        if summarizable_groups:
            try:
                summary_message = await self._summarize(_flatten(summarizable_groups))
                summarized = True
            except Exception:
                # Context compression is explicitly best-effort; main chat must
                # remain usable when the summary provider is unavailable.
                summary_failed = True

        candidate: list[ChatMessage] = _flatten(protected_groups)
        if summary_message is not None:
            candidate.append(summary_message)
        candidate.extend(_flatten(recent_groups))

        fitted, hard_dropped = self._fit_budget(candidate)
        summarized_source_count = len(_flatten(summarizable_groups))
        dropped = hard_dropped + (
            summarized_source_count if summary_message is None else max(0, summarized_source_count - 1)
        )
        after = self.count_messages(fitted)
        return ContextPreparation(
            messages=tuple(fitted),
            token_count_before=before,
            token_count_after=after,
            changed=tuple(fitted) != original,
            summarized=summarized,
            summary_failed=summary_failed,
            dropped_messages=dropped,
        )

    async def _summarize(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        transcript = _transcript(messages)
        prompt = ChatMessage(
            role="user",
            content=(
                "Summarize the earlier conversation for another assistant. Preserve user "
                "goals, decisions, constraints, unresolved questions, important facts, and "
                "tool findings. Do not add facts. Be concise.\n\n"
                f"<conversation>\n{transcript}\n</conversation>"
            ),
            metadata={"kind": "summary_request"},
        )
        response = await self.clients.get_client("summary").complete(
            [prompt], tools=None, max_tokens=self.config.summary_max_tokens
        )
        content = response.content.strip()
        if not content:
            raise ValueError("Summary model returned empty content")
        return ChatMessage(
            role="user",
            content=f"<conversation_summary>\n{content}\n</conversation_summary>",
            metadata={"kind": "context_summary"},
        )

    def _fit_budget(self, messages: Sequence[ChatMessage]) -> tuple[list[ChatMessage], int]:
        fitted = list(messages)
        dropped = 0
        groups = _message_groups(fitted)
        while self.count_messages(_flatten(groups)) > self.config.token_budget and len(groups) > 1:
            removable = next(
                (
                    index
                    for index, group in enumerate(groups[:-1])
                    if not _is_protected_group(group)
                ),
                None,
            )
            if removable is None:
                break
            dropped += len(groups[removable])
            groups.pop(removable)
        fitted = _flatten(groups)
        if self.count_messages(fitted) <= self.config.token_budget:
            return fitted, dropped

        # Exceptional case: a single recent message or protected skill exceeds
        # the entire budget. Keep message structure and trim textual bodies.
        overflow = self.count_messages(fitted) - self.config.token_budget
        for index in range(len(fitted)):
            if overflow <= 0:
                break
            message = fitted[index]
            content = message.content or ""
            if not content:
                continue
            removable_chars = min(len(content), overflow * 4 + 64)
            if removable_chars >= len(content):
                replacement = "[content truncated to fit context budget]"
            else:
                replacement = (
                    "[earlier content truncated]\n" + content[removable_chars:]
                )
            fitted[index] = replace(
                message,
                content=replacement,
                metadata={**dict(message.metadata), "context_truncated": True},
            )
            overflow = self.count_messages(fitted) - self.config.token_budget
        return fitted, dropped

    async def generate_title(
        self, messages: Sequence[ChatMessage], *, max_chars: int = 48
    ) -> str:
        fallback = _fallback_title(messages, max_chars=max_chars)
        transcript = _transcript(
            [
                message
                for message in messages
                if message.role in ("user", "assistant")
                and message.metadata.get("kind") not in {"skill_injection", "context_summary"}
            ]
        )[:6_000]
        if not transcript.strip():
            return fallback
        prompt = ChatMessage(
            role="user",
            content=(
                "Create a specific 3-8 word title for this conversation. Return only the "
                f"title, with no quotation marks.\n\n{transcript}"
            ),
            metadata={"kind": "title_request"},
        )
        try:
            response = await self.clients.get_client("summary").complete(
                [prompt], tools=None, max_tokens=self.config.title_max_tokens
            )
            title = _clean_title(response.content, max_chars=max_chars)
            return title or fallback
        except Exception:
            return fallback


def _message_groups(messages: Sequence[ChatMessage]) -> list[list[ChatMessage]]:
    """Keep assistant tool requests and all following tool replies atomic."""

    groups: list[list[ChatMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        index += 1
        if message.role == "assistant" and message.tool_calls:
            pending = {call.id for call in message.tool_calls}
            while index < len(messages) and messages[index].role == "tool":
                reply = messages[index]
                group.append(reply)
                if reply.tool_call_id:
                    pending.discard(reply.tool_call_id)
                index += 1
                if not pending:
                    break
        groups.append(group)
    return groups


def _recent_group_start(groups: Sequence[Sequence[ChatMessage]], minimum: int) -> int:
    count = 0
    for index in range(len(groups) - 1, -1, -1):
        count += len(groups[index])
        if count >= minimum:
            return index
    return 0


def _is_protected_group(group: Sequence[ChatMessage]) -> bool:
    return any(
        message.role == "system" or message.metadata.get("kind") == "skill_injection"
        for message in group
    )


def _flatten(groups: Sequence[Sequence[ChatMessage]]) -> list[ChatMessage]:
    return [message for group in groups for message in group]


def _transcript(messages: Sequence[ChatMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        if message.metadata.get("kind") == "skill_injection":
            continue
        content = (message.content or "").strip()
        if content:
            lines.append(f"{message.role.upper()}: {content}")
        for call in message.tool_calls:
            lines.append(f"ASSISTANT TOOL CALL: {call.name}({dict(call.arguments)!r})")
    return "\n".join(lines)


def _fallback_title(messages: Sequence[ChatMessage], *, max_chars: int) -> str:
    for message in messages:
        if message.role == "user" and message.metadata.get("kind") not in {
            "skill_injection",
            "context_summary",
        }:
            text = re.sub(r"\s+", " ", message.content or "").strip()
            if text:
                return text[:max_chars].rstrip()
    return "New conversation"


def _clean_title(value: str, *, max_chars: int) -> str:
    title = re.sub(r"\s+", " ", value).strip().strip("\"'`“”‘’")
    title = re.sub(r"^(title|标题)\s*:\s*", "", title, flags=re.IGNORECASE)
    return title[:max_chars].rstrip()

