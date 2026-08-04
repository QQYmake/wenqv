"""Meta-tools through which the model manages per-session skills."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..registry import Tool, ToolExecutionContext, ToolOutput
from ..skills import SkillManager


def load_skill_tool(manager: SkillManager) -> Tool:
    names = [skill.name for skill in manager.list()]
    catalogue = "; ".join(
        f"{skill.name}: {skill.description}" for skill in manager.list()
    ) or "(no skills registered)"

    async def execute(
        arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolOutput:
        name = str(arguments["name"])
        manager.get(name)
        loaded = await context.store.list_session_skills(context.session_id)
        if name in loaded:
            return ToolOutput(
                {"status": "already_loaded", "name": name},
                {"skill_action": "noop", "skill_name": name},
            )
        return ToolOutput(
            {
                "status": "loaded",
                "name": name,
                "context": manager.render(name),
            },
            {"skill_action": "load", "skill_name": name},
        )

    property_schema: dict[str, Any] = {
        "type": "string",
        "description": f"Exact skill name. Available skills: {catalogue}",
    }
    if names:
        property_schema["enum"] = names
    return Tool(
        name="load_skill",
        description=(
            "Load one relevant skill into this conversation. Repeated loads are "
            f"deduplicated. Available skills: {catalogue}"
        ),
        parameters={
            "type": "object",
            "properties": {"name": property_schema},
            "required": ["name"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def remove_skill_tool(manager: SkillManager) -> Tool:
    names = [skill.name for skill in manager.list()]

    async def execute(
        arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolOutput:
        name = str(arguments["name"])
        manager.get(name)
        loaded = await context.store.list_session_skills(context.session_id)
        if name not in loaded:
            return ToolOutput(
                {"status": "not_loaded", "name": name},
                {"skill_action": "noop", "skill_name": name},
            )
        return ToolOutput(
            {"status": "removed", "name": name},
            {"skill_action": "remove", "skill_name": name},
        )

    property_schema: dict[str, Any] = {
        "type": "string",
        "description": "Exact name of the skill to remove.",
    }
    if names:
        property_schema["enum"] = names
    return Tool(
        name="remove_skill",
        description="Remove a previously loaded skill from future conversation context.",
        parameters={
            "type": "object",
            "properties": {"name": property_schema},
            "required": ["name"],
            "additionalProperties": False,
        },
        executor=execute,
    )
