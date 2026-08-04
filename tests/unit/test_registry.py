from __future__ import annotations

import asyncio
import json

from server.agent.memory import InMemoryConversationStore
from server.agent.registry import Tool, ToolExecutionContext, ToolRegistry
from server.agent.tools import calculator_tool, read_file_tool


def _context(tmp_path) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="session",
        store=InMemoryConversationStore(),
        workspace_root=tmp_path,
        request_id="request",
    )


def test_calculator_and_argument_validation(tmp_path) -> None:
    async def scenario() -> None:
        registry = ToolRegistry([calculator_tool()])
        result = await registry.execute(
            "calculator",
            {"expression": "(17 + 5) * 3"},
            _context(tmp_path),
            timeout_s=1,
            max_result_chars=1_000,
        )
        assert not result.error
        assert json.loads(result.content)["value"] == 66

        invalid = await registry.execute(
            "calculator",
            {},
            _context(tmp_path),
            timeout_s=1,
            max_result_chars=1_000,
        )
        assert invalid.error
        assert json.loads(invalid.content)["error"] is True

    asyncio.run(scenario())


def test_tool_errors_timeouts_and_truncation_are_standardized(tmp_path) -> None:
    async def explode(_arguments, _context):
        raise RuntimeError("broken")

    async def slow(_arguments, _context):
        await asyncio.sleep(1)

    async def verbose(_arguments, _context):
        return "x" * 1_000

    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def scenario() -> None:
        registry = ToolRegistry(
            [
                Tool("explode", "fail", schema, explode),
                Tool("slow", "wait", schema, slow),
                Tool("verbose", "long", schema, verbose),
            ]
        )
        failed = await registry.execute(
            "explode", {}, _context(tmp_path), timeout_s=1, max_result_chars=200
        )
        assert failed.error and json.loads(failed.content)["message"] == "broken"

        timed_out = await registry.execute(
            "slow", {}, _context(tmp_path), timeout_s=0.01, max_result_chars=200
        )
        assert timed_out.error and "timed out" in timed_out.content

        truncated = await registry.execute(
            "verbose", {}, _context(tmp_path), timeout_s=1, max_result_chars=100
        )
        assert truncated.truncated and len(truncated.content) == 100
        assert "original_chars=1000" in truncated.content

        unknown = await registry.execute(
            "missing", {}, _context(tmp_path), timeout_s=1, max_result_chars=200
        )
        assert unknown.error and "Unknown tool" in unknown.content

    asyncio.run(scenario())


def test_read_file_is_confined_to_workspace(tmp_path) -> None:
    inside = tmp_path / "note.txt"
    inside.write_text("lake", encoding="utf-8")
    outside = tmp_path.parent / "outside-agent-test.txt"
    outside.write_text("secret", encoding="utf-8")

    async def scenario() -> None:
        registry = ToolRegistry([read_file_tool()])
        success = await registry.execute(
            "read_file",
            {"path": "note.txt"},
            _context(tmp_path),
            timeout_s=1,
            max_result_chars=1_000,
        )
        assert json.loads(success.content)["content"] == "lake"

        denied = await registry.execute(
            "read_file",
            {"path": str(outside)},
            _context(tmp_path),
            timeout_s=1,
            max_result_chars=1_000,
        )
        assert denied.error and "outside" in denied.content

    try:
        asyncio.run(scenario())
    finally:
        outside.unlink(missing_ok=True)

