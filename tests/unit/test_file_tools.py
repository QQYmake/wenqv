from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from server.agent.memory import InMemoryConversationStore
from server.agent.registry import ToolExecutionContext, ToolOutput
from server.agent.tools import edit_tool, find_tool, grep_tool, ls_tool, read_tool, write_tool


def context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="session",
        store=InMemoryConversationStore(),
        workspace_root=root,
        request_id="request",
    )


def run(tool, root: Path, arguments: dict):
    return asyncio.run(tool.executor(arguments, context(root)))


def test_read_pages_text_and_reports_offset_errors(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = run(read_tool(), tmp_path, {"path": "large.txt", "offset": 2, "limit": 1})
    assert result.startswith("two")
    assert "[Output truncated" in result
    with pytest.raises(ValueError, match="Offset 9 is beyond end of file"):
        run(read_tool(), tmp_path, {"path": "large.txt", "offset": 9})


def test_read_resizes_image_and_returns_ephemeral_attachment(tmp_path: Path) -> None:
    image_path = tmp_path / "large.gif"
    Image.new("RGB", (3_000, 1_000), "blue").save(image_path, format="GIF")
    result = run(read_tool(), tmp_path, {"path": "large.gif"})
    assert isinstance(result, ToolOutput)
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert (attachment.width, attachment.height) == (1_568, 523)
    assert attachment.media_type == "image/png"
    assert attachment.data_url.startswith("data:image/png;base64,")


def test_write_creates_parents_counts_utf8_and_honors_cancel(tmp_path: Path) -> None:
    tool = write_tool()
    result = run(tool, tmp_path, {"path": "nested/note.txt", "content": "湖"})
    assert result == "Successfully wrote 3 bytes to nested/note.txt"
    assert (tmp_path / "nested/note.txt").read_text(encoding="utf-8") == "湖"

    cancel = asyncio.Event()
    cancel.set()
    cancelled = context(tmp_path)
    cancelled = ToolExecutionContext(
        session_id=cancelled.session_id,
        store=cancelled.store,
        workspace_root=cancelled.workspace_root,
        request_id=cancelled.request_id,
        cancel_event=cancel,
    )
    with pytest.raises(RuntimeError, match="Operation aborted"):
        asyncio.run(tool.executor({"path": "nested/note.txt", "content": "changed"}, cancelled))
    assert (tmp_path / "nested/note.txt").read_text(encoding="utf-8") == "湖"


def test_edit_is_atomic_unique_and_emits_patch(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = run(
        edit_tool(),
        tmp_path,
        {
            "path": "app.py",
            "edits": [
                {"oldText": "alpha", "newText": "ALPHA"},
                {"oldText": "gamma", "newText": "GAMMA"},
            ],
        },
    )
    assert isinstance(result, ToolOutput)
    assert result.value == "Successfully replaced 2 block(s) in app.py"
    assert "--- a/app.py" in result.metadata["ui_patch"]
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n"

    target.write_text("same same", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        run(
            edit_tool(),
            tmp_path,
            {"path": "app.py", "edits": [{"oldText": "same", "newText": "x"}]},
        )
    assert target.read_text(encoding="utf-8") == "same same"


def test_grep_find_and_ls_follow_ignore_glob_context_and_limits(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("visible", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("before\nNeedle value\nafter\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "secret.ts").write_text("Needle secret", encoding="utf-8")
    (tmp_path / "debug.log").write_text("Needle log", encoding="utf-8")

    searched = run(
        grep_tool(),
        tmp_path,
        {"pattern": "needle", "ignoreCase": True, "glob": "**/*.ts", "context": 1},
    )
    assert "src/app.ts:2:Needle value" in searched
    assert "src/app.ts-1-before" in searched
    assert "secret" not in searched and "debug.log" not in searched

    found = run(find_tool(), tmp_path, {"pattern": "**/*.ts"})
    assert found == "src/app.ts"
    assert "ignored" not in found

    listed = run(ls_tool(), tmp_path, {})
    assert ".gitignore" in listed and ".hidden" in listed and "src/" in listed
    limited = run(ls_tool(), tmp_path, {"limit": 1})
    assert "1 entries limit reached" in limited


def test_all_file_tools_reject_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-file-tools.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        cases = [
            (read_tool(), {"path": str(outside)}),
            (write_tool(), {"path": str(outside), "content": "x"}),
            (edit_tool(), {"path": str(outside), "edits": [{"oldText": "secret", "newText": "x"}]}),
            (grep_tool(), {"pattern": "secret", "path": str(outside)}),
            (find_tool(), {"pattern": "*", "path": str(outside)}),
            (ls_tool(), {"path": str(outside)}),
        ]
        for tool, arguments in cases:
            with pytest.raises(ValueError, match="outside the active workspace"):
                run(tool, tmp_path, arguments)
    finally:
        outside.unlink(missing_ok=True)
