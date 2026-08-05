"""Unit tests for traversal defenses in the resolver and the read_file tool.

The workspace resolver maps ids to private directories; the read_file tool
must never resolve a path outside the active workspace root, regardless of
the attack spelling: POSIX or Windows separators, absolute paths, encoded
dot-dot sequences, or symlinks pointing out of the root.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from server.agent.memory import InMemoryConversationStore
from server.agent.registry import ToolExecutionContext
from server.agent.tools.read_file import read_file_tool
from server.storage import IsolatedWorkspaceResolver


@pytest.mark.parametrize(
    "bad_id",
    [
        "/etc/passwd",          # absolute POSIX path
        r"C:\Windows\win.ini",  # absolute Windows path
        "a/b",                  # POSIX separator
        r"a\b",                 # Windows separator
        "ws-alpha/../ws-beta",  # traversal through a child
        "%2e%2e",               # percent-encoded dot-dot
        "%2e%2e%2fetc",         # encoded traversal
        "x" * 200,              # overlong id (regex caps at 128)
    ],
)
def test_workspace_resolver_rejects_malicious_ids(tmp_path, bad_id: str) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    with pytest.raises(ValueError):
        resolver(bad_id)


def _context(root) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="s1",
        store=InMemoryConversationStore(),
        workspace_root=root,
        request_id="r1",
        workspace_id="ws-alpha",
    )


def _read(tool, context, path: str) -> dict:
    return asyncio.run(tool.executor({"path": path}, context))


def test_read_file_rejects_relative_and_absolute_escapes(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    alpha_root = resolver("ws-alpha")
    tool = read_file_tool()
    context = _context(alpha_root)

    # A file outside the workspace (sibling workspace) as an absolute path.
    beta_file = resolver("ws-beta") / "secret.txt"
    beta_file.write_text("beta-secret", encoding="utf-8")
    for path in (str(beta_file), "/etc/passwd"):
        with pytest.raises(ValueError, match="outside the active workspace"):
            _read(tool, context, path)

    # Pure traversal chains relative to the workspace root.
    for path in ("..", "../..", "../../../etc/passwd", "../ws-beta/secret.txt"):
        with pytest.raises(ValueError, match="outside the active workspace"):
            _read(tool, context, path)


def test_read_file_never_escapes_via_windows_style_or_encoded_paths(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    alpha_root = resolver("ws-alpha")
    beta_root = resolver("ws-beta")
    beta_secret = beta_root / "secret.txt"
    beta_secret.write_text("beta-secret", encoding="utf-8")

    tool = read_file_tool()
    context = _context(alpha_root)

    # Windows-style traversal: on Windows the backslash is a separator and the
    # guard rejects it; on POSIX it is a literal filename that must never
    # resolve to the sibling's file (FileNotFoundError at worst).
    windows_variants = [
        r"..\ws-beta\secret.txt",
        r"..\..\ws-beta\secret.txt",
        r"C:\Windows\win.ini",
    ]
    # Encoded dot-dot is never decoded by the path guard; the request must not
    # be able to address the sibling file under any spelling.
    encoded_variants = [
        "%2e%2e/ws-beta/secret.txt",
        "..%2fws-beta%2fsecret.txt",
        "..%5cws-beta%5csecret.txt",
    ]
    for path in windows_variants + encoded_variants:
        with pytest.raises((ValueError, FileNotFoundError)):
            result = _read(tool, context, path)
            # Defense in depth: even if no exception were raised, the result
            # must never contain the other workspace's content.
            assert "beta-secret" not in result.get("content", "")


def test_read_file_positive_control_inside_workspace(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    alpha_root = resolver("ws-alpha")
    notes = alpha_root / "notes.txt"
    notes.write_text("alpha-secret", encoding="utf-8")

    tool = read_file_tool()
    result = _read(tool, _context(alpha_root), "notes.txt")
    assert result["content"] == "alpha-secret"
    assert result["path"] == "notes.txt"


def test_read_file_rejects_symlink_escape(tmp_path) -> None:
    resolver = IsolatedWorkspaceResolver(tmp_path)
    alpha_root = resolver("ws-alpha")
    beta_root = resolver("ws-beta")
    beta_secret = beta_root / "secret.txt"
    beta_secret.write_text("beta-secret", encoding="utf-8")

    link = alpha_root / "escape.txt"
    try:
        os.symlink(beta_secret, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    tool = read_file_tool()
    with pytest.raises(ValueError, match="outside the active workspace"):
        _read(tool, _context(alpha_root), "escape.txt")
