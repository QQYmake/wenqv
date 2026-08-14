from __future__ import annotations

import asyncio

import pytest

from server.agent.memory import InMemoryConversationStore
from server.agent.skills import SkillError, SkillManager, SkillNotFoundError
from server.agent.registry import ToolExecutionContext
from server.agent.tools import load_skill_tool, remove_skill_tool


def _write_skill(directory, name="planning", description="Plan carefully") -> None:
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nKeep scope bounded.\n",
        encoding="utf-8",
    )


def _write_packaged_skill(
    directory, name="wenqu", description="Persistent training coach"
) -> None:
    skill_dir = directory / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nKeep state in the workspace.\n",
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


def test_scan_supports_packaged_and_legacy_flat_skills(tmp_path) -> None:
    _write_skill(tmp_path)
    _write_packaged_skill(tmp_path)

    manager = SkillManager(tmp_path)

    assert [item["name"] for item in manager.catalog()] == ["planning", "wenqu"]
    assert manager.get("wenqu").path == tmp_path / "wenqu" / "SKILL.md"


def test_packaged_skill_folder_must_match_frontmatter_name(tmp_path) -> None:
    skill_dir = tmp_path / "wrong-folder"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: right-name\ndescription: Test\n---\n\nInstructions.\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillError, match="must match"):
        SkillManager(tmp_path)


def test_duplicate_names_across_flat_and_packaged_skills_fail(tmp_path) -> None:
    _write_skill(tmp_path, name="planning")
    _write_packaged_skill(tmp_path, name="planning")

    with pytest.raises(SkillError, match="Duplicate skill name"):
        SkillManager(tmp_path)


def test_runtime_context_is_marked_in_injected_skill(tmp_path) -> None:
    _write_packaged_skill(tmp_path)
    manager = SkillManager(tmp_path)
    store = InMemoryConversationStore()

    async def scenario() -> None:
        results = await manager.inject_selected(
            store,
            "conversation-1",
            ["wenqu"],
            runtime_context={
                "conversation_id": "conversation-1",
                "workspace_data_root": "wenqu/sessions",
            },
        )
        assert results[0].loaded
        content = (await store.list_messages("conversation-1"))[0].content
        assert "<runtime_context>" in content
        assert 'conversation_id: "conversation-1"' in content
        assert 'workspace_data_root: "wenqu/sessions"' in content

    asyncio.run(scenario())


def test_remove_skill_protects_configured_defaults(tmp_path) -> None:
    _write_packaged_skill(tmp_path)
    manager = SkillManager(tmp_path)
    store = InMemoryConversationStore()

    async def scenario() -> None:
        await manager.inject_selected(store, "s1", ["wenqu"])
        context = ToolExecutionContext(
            session_id="s1",
            store=store,
            workspace_root=tmp_path,
            request_id="request-1",
        )
        output = await remove_skill_tool(
            manager, protected_names=("wenqu",)
        ).executor({"name": "wenqu"}, context)
        assert output.value == {"status": "protected", "name": "wenqu"}
        assert await store.list_session_skills("s1") == {"wenqu"}

    asyncio.run(scenario())


def test_invalid_skill_frontmatter_fails_at_scan(tmp_path) -> None:
    (tmp_path / "bad.md").write_text("No frontmatter", encoding="utf-8")
    with pytest.raises(SkillError, match="frontmatter"):
        SkillManager(tmp_path)
