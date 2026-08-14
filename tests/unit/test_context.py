from __future__ import annotations

import asyncio

from server.agent.context import ContextConfig, ContextManager
from server.agent.models import ChatMessage, LLMResponse


class SummaryClient:
    def __init__(self, *, response="Concise earlier context", fail=False):
        self.response = response
        self.fail = fail
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None):
        self.calls.append((messages, tools, max_tokens))
        if self.fail:
            raise RuntimeError("summary unavailable")
        return LLMResponse(self.response)

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        if False:
            yield


class Provider:
    def __init__(self, main, summary=None):
        self.main = main
        self.summary = summary or main

    def get_client(self, role, workspace_id=None):
        return self.main if role == "main" else self.summary


def _history() -> list[ChatMessage]:
    return [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"message-{i}-" + "x" * 30)
        for i in range(10)
    ]


def test_over_budget_history_is_summarized_by_summary_role() -> None:
    client = SummaryClient()
    manager = ContextManager(
        Provider(SummaryClient(), client),
        ContextConfig(token_budget=180, summary_trigger_ratio=0.5, min_recent_messages=2),
        token_counter=len,
    )

    result = asyncio.run(manager.prepare(_history()))

    assert result.changed and result.summarized and not result.summary_failed
    assert any(m.metadata.get("kind") == "context_summary" for m in result.messages)
    assert result.messages[-1].content.startswith("message-9")
    assert client.calls and client.calls[0][1] is None
    assert result.token_count_after <= 180


def test_summary_failure_degrades_to_truncation_without_raising() -> None:
    failing = SummaryClient(fail=True)
    manager = ContextManager(
        Provider(SummaryClient(), failing),
        ContextConfig(token_budget=180, summary_trigger_ratio=0.5, min_recent_messages=2),
        token_counter=len,
    )

    result = asyncio.run(manager.prepare(_history()))

    assert result.changed and result.summary_failed and not result.summarized
    assert result.messages[-1].content.startswith("message-9")
    assert result.token_count_after <= 180


def test_skill_context_is_preserved_during_compaction() -> None:
    client = SummaryClient()
    messages = _history()
    messages.insert(
        1,
        ChatMessage(
            role="user",
            content="important skill instructions",
            metadata={"kind": "skill_injection", "skill_name": "planning"},
        ),
    )
    manager = ContextManager(
        Provider(client),
        ContextConfig(token_budget=220, summary_trigger_ratio=0.5, min_recent_messages=2),
        token_counter=len,
    )

    result = asyncio.run(manager.prepare(messages))
    assert any(m.metadata.get("skill_name") == "planning" for m in result.messages)


def test_title_uses_summary_model_and_falls_back_on_failure() -> None:
    title_client = SummaryClient(response='"Lake tool workflow"')
    manager = ContextManager(Provider(SummaryClient(), title_client))
    messages = [ChatMessage(role="user", content="Analyze the lake workflow in detail")]

    assert asyncio.run(manager.generate_title(messages)) == "Lake tool workflow"

    fallback_manager = ContextManager(Provider(SummaryClient(fail=True)))
    assert (
        asyncio.run(fallback_manager.generate_title(messages, max_chars=20))
        == "Analyze the lake wor"
    )
