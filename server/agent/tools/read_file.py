"""Workspace-confined text-file reader."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registry import Tool, ToolExecutionContext


def _resolve_inside(root: Path, requested: str) -> Path:
    root = root.resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path is outside the active workspace") from exc
    return candidate


def read_file_tool(*, max_bytes: int = 1_000_000) -> Tool:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    async def execute(
        arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        requested = str(arguments["path"])
        encoding = str(arguments.get("encoding", "utf-8"))
        path = _resolve_inside(context.workspace_root, requested)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {requested}")
        if not path.is_file():
            raise ValueError(f"Path is not a regular file: {requested}")
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        try:
            content = data.decode(encoding)
        except LookupError as exc:
            raise ValueError(f"Unknown text encoding: {encoding}") from exc
        except UnicodeDecodeError:
            content = data.decode(encoding, errors="replace")
        return {
            "path": path.relative_to(context.workspace_root.resolve()).as_posix(),
            "content": content,
            "truncated": truncated,
            "size_bytes": size,
        }

    return Tool(
        name="read_file",
        description="Read a UTF text file located inside the active workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root.",
                },
                "encoding": {
                    "type": "string",
                    "description": "Text encoding; defaults to utf-8.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        executor=execute,
    )

