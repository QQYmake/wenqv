from __future__ import annotations

import asyncio

from server.agent.models import ChatMessage, ToolCall
from server.storage import AgentStoreAdapter, MemoryTTLCache, SQLiteStore, SideCache


def run(coroutine):
    return asyncio.run(coroutine)


def test_sqlite_store_isolates_workspaces_and_persists_metadata():
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        first = await store.create_session("alpha", "Alpha")
        await store.create_session("beta", "Beta")
        message = await store.add_message(
            first["id"],
            "assistant",
            "result",
            kind="tool_result",
            name="calculator",
            metadata={"call_id": "call-1", "error": False},
        )

        assert [item["title"] for item in await store.list_sessions("alpha")] == [
            "Alpha"
        ]
        assert await store.get_session(first["id"], "beta") is None
        restored = await store.list_messages(first["id"], "alpha")
        assert restored is not None
        assert restored[0]["id"] == message["id"]
        assert restored[0]["metadata"]["call_id"] == "call-1"
        await store.close()

    run(scenario())


def test_agent_store_round_trips_tools_and_deduplicates_skills():
    async def scenario():
        raw = SQLiteStore(":memory:")
        await raw.initialize()
        session = await raw.create_session("default")
        store = AgentStoreAdapter(raw)
        await store.add_message(
            session["id"],
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=(ToolCall("call-1", "calculator", {"expression": "2+2"}),),
            ),
        )
        await store.add_message(
            session["id"],
            ChatMessage(
                role="tool",
                content="4",
                tool_call_id="call-1",
                name="calculator",
            ),
        )
        injection = ChatMessage(
            role="user",
            content='<skill_context name="demo">demo</skill_context>',
            metadata={"kind": "skill_injection", "skill_name": "demo"},
        )
        assert await store.inject_skill(session["id"], "demo", injection) is True
        assert await store.inject_skill(session["id"], "demo", injection) is False

        messages = await store.list_messages(session["id"])
        assert messages[0].tool_calls[0].name == "calculator"
        assert messages[1].tool_call_id == "call-1"
        assert await store.list_session_skills(session["id"]) == {"demo"}

        await store.replace_messages(session["id"], messages)
        assert await store.list_session_skills(session["id"]) == {"demo"}
        assert await store.remove_session_skill(session["id"], "demo") is True
        assert await store.list_session_skills(session["id"]) == set()
        await raw.close()

    run(scenario())


def test_memory_database_uses_shared_connections():
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        session = await store.create_session("default")
        await store.add_message(session["id"], "user", "hello")
        assert len(await store.list_sessions("default")) == 1
        assert len(await store.list_messages(session["id"]) or ()) == 1
        await store.close()

    run(scenario())


def test_redis_failure_degrades_to_memory_without_losing_cached_value():
    class FailingPrimary:
        async def get(self, key):
            raise ConnectionError("redis unavailable")

        async def set(self, key, value, ttl_s=None):
            raise ConnectionError("redis unavailable")

        async def delete(self, key):
            raise ConnectionError("redis unavailable")

        async def delete_prefix(self, prefix):
            raise ConnectionError("redis unavailable")

        async def close(self):
            return None

    async def scenario():
        cache = SideCache(MemoryTTLCache(), FailingPrimary())
        await cache.set("dynamic", {"value": 7})
        assert await cache.get("dynamic") == {"value": 7}
        await cache.close()

    run(scenario())
