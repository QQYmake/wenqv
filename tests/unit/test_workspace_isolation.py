"""Unit tests for the per-workspace file isolation resolver and read_file."""

from __future__ import annotations

import pytest

from server.agent.tools.read_file import read_file_tool
from server.agent.registry import ToolExecutionContext
from server.agent.memory import InMemoryConversationStore
from server.storage import IsolatedWorkspaceResolver


def test_workspace_resolver_returns_isolated_root_per_workspace(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)

    alpha = resolver("ws-alpha")
    beta = resolver("ws-beta")
    shared = resolver(None)

    assert alpha != beta
    assert alpha == (tmp_path / "ws-alpha").resolve()
    assert beta == (tmp_path / "ws-beta").resolve()
    assert shared == tmp_path.resolve()
    # Directories are created lazily on first access.
    assert alpha.is_dir() and beta.is_dir()


def test_workspace_resolver_rejects_traversal(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    with pytest.raises(ValueError):
        resolver("..")
    with pytest.raises(ValueError):
        resolver("ws/../..")
    with pytest.raises(ValueError):
        resolver(" spacey id")


def test_read_file_rejects_path_outside_workspace_root(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)

    alpha_root = resolver("ws-alpha")
    # A file inside alpha's own dir is readable.
    secret = alpha_root / "notes.txt"
    secret.write_text("alpha-secret", encoding="utf-8")

    # A file belonging to another workspace (or any path outside alpha root).
    other_root = resolver("ws-beta")
    other_file = other_root / "secret.txt"
    other_file.write_text("beta-secret", encoding="utf-8")

    store = InMemoryConversationStore()
    tool = read_file_tool()
    ctx_alpha = ToolExecutionContext(
        session_id="s1",
        store=store,
        workspace_root=alpha_root,
        request_id="r1",
        workspace_id="ws-alpha",
    )

    import asyncio

    async def run_alpha() -> dict:
        return await tool.executor({"path": "notes.txt"}, ctx_alpha)

    result = asyncio.run(run_alpha())
    assert result["content"] == "alpha-secret"

    # An absolute path pointing at the other workspace is rejected.
    async def run_cross() -> None:
        await tool.executor({"path": str(other_file)}, ctx_alpha)

    with pytest.raises(ValueError, match="outside the active workspace"):
        asyncio.run(run_cross())

    # A relative traversal attempt is also rejected.
    async def run_traverse() -> None:
        await tool.executor({"path": "../ws-beta/secret.txt"}, ctx_alpha)

    with pytest.raises(ValueError, match="outside the active workspace"):
        asyncio.run(run_traverse())