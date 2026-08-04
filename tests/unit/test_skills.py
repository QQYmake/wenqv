from __future__ import annotations

import asyncio

import pytest

from server.agent.memory import InMemoryConversationStore
from server.agent.skills import SkillError, SkillManager, SkillNotFoundError
from server.agent.tools import load_skill_tool


def _write_skill(directory, name="planning", description="Plan carefully") -> None:
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nKeep scope bounded.\n",
        encoding="utf-8",
    )


def test_scan_mentions_and_marked_rendering(tmp_path) -> None:
    _write_skill(tmp_path)
    manager = SkillManager(tmp_path)

    assert manager.catalog() == [{"name": "planning", "description": "Plan carefully"}]
    assert manager.extract_mentions("please use @planning now") == ("planning",)
    assert manager.extract_mentions("unknown @missing") == ()
    rendered = manager.render("planning")
    assert '<skill_context name="planning">' in rendered
    assert "Keep scope bounded." in rendered
    schema = load_skill_tool(manager).schema()["function"]
    assert "planning: Plan carefully" in schema["description"]
    assert "planning: Plan carefully" in schema["parameters"]["properties"]["name"]["description"]
    with pytest.raises(SkillNotFoundError):
        manager.get("missing")


def test_persisted_per_session_deduplication_and_removal(tmp_path) -> None:
    _write_skill(tmp_path)
    manager = SkillManager(tmp_path)
    store = InMemoryConversationStore()

    async def scenario() -> None:
        first = await manager.inject_selected(store, "s1", ["planning", "planning"])
        second = await manager.inject_selected(store, "s1", ["planning"])
        assert len(first) == 1 and first[0].loaded
        assert second[0].already_loaded
        assert await store.list_session_skills("s1") == {"planning"}
        messages = await store.list_messages("s1")
        assert sum(m.metadata.get("kind") == "skill_injection" for m in messages) == 1

        assert await store.remove_session_skill("s1", "planning")
        assert await store.list_session_skills("s1") == set()
        third = await manager.inject_selected(store, "s1", ["planning"])
        assert third[0].loaded

    asyncio.run(scenario())


def test_invalid_skill_frontmatter_fails_at_scan(tmp_path) -> None:
    (tmp_path / "bad.md").write_text("No frontmatter", encoding="utf-8")
    with pytest.raises(SkillError, match="frontmatter"):
        SkillManager(tmp_path)
