"""Workspace-confined file tools exposed to every Agent run."""

from __future__ import annotations

import base64
import difflib
import fnmatch
import io
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..models import ImageAttachment
from ..registry import Tool, ToolExecutionContext, ToolOutput


MAX_OUTPUT_BYTES = 50 * 1024
MAX_READ_LINES = 2_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_EDGE = 1_568
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_TRUNCATED = "\n[Output truncated at 2000 lines / 50KB. Use offset/limit to continue.]"


def resolve_inside(root: Path, requested: str, *, must_exist: bool = True) -> Path:
    """Resolve relative/absolute paths while enforcing the active workspace."""

    workspace = root.resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("Path is outside the active workspace") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"File not found: {requested}")
    return candidate


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root.resolve()).as_posix()


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def _nonnegative_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative")
    return parsed


def _check_cancel(context: ToolExecutionContext) -> None:
    if context.cancel_event is not None and context.cancel_event.is_set():
        raise RuntimeError("Operation aborted")


def _clip_utf8(text: str, marker: str = "\n[Output truncated at 50KB]") -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    marker_bytes = marker.encode("utf-8")
    prefix = encoded[: max(0, MAX_OUTPUT_BYTES - len(marker_bytes))]
    return prefix.decode("utf-8", errors="ignore") + marker


def _append_within_limit(text: str, marker: str) -> str:
    marker_bytes = marker.encode("utf-8")
    prefix = text.encode("utf-8")[: max(0, MAX_OUTPUT_BYTES - len(marker_bytes))]
    return prefix.decode("utf-8", errors="ignore") + marker


def _atomic_write(path: Path, content: str, context: ToolExecutionContext) -> int:
    _check_cancel(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ValueError(f"Path is not a regular file: {path}")
    data = content.encode("utf-8")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _check_cancel(context)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return len(data)


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Path is not a regular file: {path}")
    data = path.read_bytes()
    if b"\x00" in data[:8_192]:
        raise ValueError(f"File is binary and cannot be read as text: {path.name}")
    return data.decode("utf-8", errors="replace")


def _image_attachment(path: Path, root: Path) -> ImageAttachment:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Pillow is required to read image files") from exc

    with Image.open(path) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source.copy())
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        output_format, media_type = "JPEG", "image/jpeg"
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        save_args = {"quality": 90, "optimize": True}
    elif suffix == ".webp":
        output_format, media_type = "WEBP", "image/webp"
        save_args = {"quality": 90, "method": 6}
    else:
        output_format, media_type = "PNG", "image/png"
        save_args = {"optimize": True}
    buffer = io.BytesIO()
    image.save(buffer, format=output_format, **save_args)
    data = buffer.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Resized image exceeds the 8MB model input limit")
    encoded = base64.b64encode(data).decode("ascii")
    return ImageAttachment(
        path=_relative(path, root),
        data_url=f"data:{media_type};base64,{encoded}",
        media_type=media_type,
        width=image.width,
        height=image.height,
    )


def read_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> Any:
        requested = str(arguments["path"])
        path = resolve_inside(context.workspace_root, requested)
        if not path.is_file():
            raise ValueError(f"Path is not a regular file: {requested}")
        if path.suffix.lower() in _IMAGE_EXTENSIONS:
            attachment = _image_attachment(path, context.workspace_root)
            return ToolOutput(
                (
                    f"Read image {attachment.path} "
                    f"({attachment.width}x{attachment.height}, {attachment.media_type})."
                ),
                attachments=(attachment,),
            )

        offset = _positive_int(arguments.get("offset"), "offset", 1)
        limit = min(_positive_int(arguments.get("limit"), "limit", MAX_READ_LINES), MAX_READ_LINES)
        selected: list[str] = []
        total_bytes = 0
        seen = 0
        truncated = False
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for line_number, line in enumerate(handle, start=1):
                seen = line_number
                if line_number < offset:
                    continue
                if len(selected) >= limit:
                    truncated = True
                    break
                encoded = line.encode("utf-8")
                if total_bytes + len(encoded) > MAX_OUTPUT_BYTES:
                    remaining = MAX_OUTPUT_BYTES - total_bytes
                    if remaining > 0:
                        selected.append(encoded[:remaining].decode("utf-8", errors="ignore"))
                    truncated = True
                    break
                selected.append(line)
                total_bytes += len(encoded)
        if offset > seen and not (offset == 1 and seen == 0):
            raise ValueError(f"Offset {offset} is beyond end of file")
        content = "".join(selected)
        return _append_within_limit(content, _TRUNCATED) if truncated else content

    return Tool(
        name="read",
        description=(
            "Read a text file or attach a resized jpg/png/gif/webp/bmp image from "
            "the active workspace. Use offset and limit to page through large files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative or absolute file path."},
                "offset": {"type": "integer", "description": "1-based starting line."},
                "limit": {"type": "integer", "description": "Maximum lines to return."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def write_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> str:
        requested = str(arguments["path"])
        path = resolve_inside(context.workspace_root, requested, must_exist=False)
        written = _atomic_write(path, str(arguments["content"]), context)
        return f"Successfully wrote {written} bytes to {_relative(path, context.workspace_root)}"

    return Tool(
        name="write",
        description=(
            "Create or completely overwrite a UTF-8 file in the active workspace. "
            "Parent directories are created automatically; use edit for local changes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def edit_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> ToolOutput:
        requested = str(arguments["path"])
        path = resolve_inside(context.workspace_root, requested)
        original = _read_text(path)
        raw_edits = arguments["edits"]
        if not isinstance(raw_edits, list) or not raw_edits:
            raise ValueError("edits must contain at least one replacement")
        replacements: list[tuple[int, int, str]] = []
        for index, item in enumerate(raw_edits, start=1):
            if not isinstance(item, Mapping):
                raise ValueError(f"edit {index} must be an object")
            old_text = str(item.get("oldText", ""))
            new_text = str(item.get("newText", ""))
            if not old_text:
                raise ValueError(f"edit {index} oldText cannot be empty")
            matches = [match.start() for match in re.finditer(re.escape(old_text), original)]
            if not matches:
                raise ValueError(f"edit {index} oldText was not found")
            if len(matches) != 1:
                raise ValueError(f"edit {index} oldText must match exactly once")
            start = matches[0]
            replacements.append((start, start + len(old_text), new_text))
        replacements.sort(key=lambda item: item[0])
        for previous, current in zip(replacements, replacements[1:]):
            if current[0] < previous[1]:
                raise ValueError("edits must not overlap or nest")
        parts: list[str] = []
        cursor = 0
        for start, end, new_text in replacements:
            parts.extend((original[cursor:start], new_text))
            cursor = end
        parts.append(original[cursor:])
        updated = "".join(parts)
        relative = _relative(path, context.workspace_root)
        patch = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        clipped_patch = _clip_utf8(patch, "\n[Patch truncated at 50KB]")
        _atomic_write(path, updated, context)
        return ToolOutput(
            f"Successfully replaced {len(replacements)} block(s) in {relative}",
            metadata={
                "ui_patch": clipped_patch,
                "ui_patch_truncated": clipped_patch != patch,
            },
        )

    return Tool(
        name="edit",
        description=(
            "Replace one or more unique, non-overlapping text blocks in one UTF-8 "
            "file. Every oldText is matched against the original file atomically."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        "required": ["oldText", "newText"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "edits"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def _load_ignore_spec(root: Path) -> Any:
    try:
        from pathspec import GitIgnoreSpec
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pathspec is required for grep and find") from exc

    patterns: list[str] = [".git/"]
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git"]
        if ".gitignore" not in files:
            continue
        ignore_file = Path(current) / ".gitignore"
        prefix = ignore_file.parent.relative_to(root).as_posix()
        prefix = "" if prefix == "." else f"{prefix}/"
        try:
            lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            negative = stripped.startswith("!")
            body = stripped[1:] if negative else stripped
            if body.startswith("/"):
                combined = prefix + body[1:]
            elif "/" in body.rstrip("/"):
                combined = prefix + body
            else:
                combined = prefix + "**/" + body
            patterns.append(("!" if negative else "") + combined)
    return GitIgnoreSpec.from_lines(patterns)


def _iter_files(search_root: Path, workspace_root: Path) -> Iterable[Path]:
    ignore = _load_ignore_spec(workspace_root.resolve())
    if search_root.is_file():
        relative = _relative(search_root, workspace_root)
        if not ignore.match_file(relative):
            yield search_root
        return
    if not search_root.is_dir():
        raise ValueError(f"Path is not a directory: {search_root}")

    def walk(directory: Path) -> Iterable[Path]:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            return
        for entry in entries:
            try:
                relative = _relative(entry.resolve(strict=False), workspace_root)
                if entry.name == ".git" or ignore.match_file(
                    relative + ("/" if entry.is_dir() else "")
                ):
                    continue
                if entry.is_symlink() and entry.is_dir():
                    continue
                if entry.is_dir():
                    yield from walk(entry)
                elif entry.is_file():
                    # A file symlink is acceptable only when its resolved target remains
                    # inside the workspace, which _relative above enforces.
                    yield entry
            except (OSError, ValueError):
                continue

    yield from walk(search_root)


def _glob_match(relative: str, pattern: str) -> bool:
    normalized = relative.replace("\\", "/")
    return fnmatch.fnmatchcase(normalized, pattern) or (
        "/" not in pattern and fnmatch.fnmatchcase(Path(normalized).name, pattern)
    ) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:])
    )


def _display_line(path: str, number: int, line: str, *, match: bool) -> tuple[str, bool]:
    clean = line.rstrip("\r\n")
    shortened = len(clean) > 250
    if shortened:
        clean = clean[:249] + "…"
    separator = ":" if match else "-"
    return f"{path}{separator}{number}{separator}{clean}", shortened


def grep_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> str:
        requested = str(arguments.get("path", "."))
        search_root = resolve_inside(context.workspace_root, requested)
        pattern = str(arguments["pattern"])
        if not pattern:
            raise ValueError("pattern cannot be empty")
        flags = re.IGNORECASE if bool(arguments.get("ignoreCase", False)) else 0
        try:
            expression = re.compile(
                re.escape(pattern) if bool(arguments.get("literal", False)) else pattern,
                flags,
            )
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        glob = str(arguments["glob"]) if arguments.get("glob") else None
        context_lines = _nonnegative_int(arguments.get("context"), "context", 0)
        limit = _positive_int(arguments.get("limit"), "limit", 100)
        base = search_root if search_root.is_dir() else search_root.parent
        output: list[str] = []
        match_count = 0
        reached_limit = False
        shortened = False
        for path in _iter_files(search_root, context.workspace_root):
            relative = path.relative_to(base).as_posix()
            if glob and not _glob_match(relative, glob):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8_192]:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
            emitted: set[int] = set()
            for index, line in enumerate(lines):
                if expression.search(line) is None:
                    continue
                match_count += 1
                start = max(0, index - context_lines)
                end = min(len(lines), index + context_lines + 1)
                for line_index in range(start, end):
                    if line_index in emitted:
                        continue
                    rendered, was_shortened = _display_line(
                        relative,
                        line_index + 1,
                        lines[line_index],
                        match=line_index == index,
                    )
                    output.append(rendered)
                    emitted.add(line_index)
                    shortened = shortened or was_shortened
                if match_count >= limit:
                    reached_limit = True
                    break
            if reached_limit:
                break
        if not output:
            return "No matches found"
        if shortened:
            output.append("[Long lines truncated to 250 characters. Use read tool to see full lines]")
        if reached_limit:
            output.append(
                f"[{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern]"
            )
        return _clip_utf8("\n".join(output))

    return Tool(
        name="grep",
        description=(
            "Search non-ignored workspace files and return path:line:matching-line. "
            "Supports regular expressions, literal matching, globs, and context lines."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "ignoreCase": {"type": "boolean"},
                "literal": {"type": "boolean"},
                "context": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def find_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> str:
        search_root = resolve_inside(
            context.workspace_root, str(arguments.get("path", "."))
        )
        if not search_root.is_dir():
            raise ValueError(f"Path is not a directory: {search_root}")
        pattern = str(arguments["pattern"])
        if not pattern:
            raise ValueError("pattern cannot be empty")
        limit = _positive_int(arguments.get("limit"), "limit", 1_000)
        results: list[str] = []
        reached_limit = False
        for path in _iter_files(search_root, context.workspace_root):
            relative = path.relative_to(search_root).as_posix()
            if not _glob_match(relative, pattern):
                continue
            results.append(relative)
            if len(results) >= limit:
                reached_limit = True
                break
        if not results:
            return "No files found matching pattern"
        if reached_limit:
            results.append(f"[{limit} results limit reached]")
        return _clip_utf8("\n".join(results))

    return Tool(
        name="find",
        description=(
            "Find non-ignored files by glob pattern and return paths relative to the "
            "search directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def ls_tool() -> Tool:
    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> str:
        directory = resolve_inside(
            context.workspace_root, str(arguments.get("path", "."))
        )
        if not directory.is_dir():
            raise ValueError(f"Path is not a directory: {directory}")
        limit = _positive_int(arguments.get("limit"), "limit", 500)
        entries: list[str] = []
        reached_limit = False
        try:
            candidates = sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError as exc:
            raise PermissionError(f"Cannot read directory: {directory}") from exc
        for entry in candidates:
            try:
                label = entry.name + ("/" if entry.is_dir() else "")
            except OSError:
                continue
            entries.append(label)
            if len(entries) >= limit:
                reached_limit = True
                break
        if not entries:
            return "(empty directory)"
        if reached_limit:
            entries.append(
                f"[{limit} entries limit reached. Use limit={limit * 2} for more]"
            )
        return _clip_utf8("\n".join(entries))

    return Tool(
        name="ls",
        description=(
            "List one workspace directory alphabetically, including dotfiles. "
            "Directory names have a trailing slash."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        executor=execute,
    )


def file_tools() -> tuple[Tool, ...]:
    return (read_tool(), write_tool(), edit_tool(), grep_tool(), find_tool(), ls_tool())


__all__ = [
    "edit_tool",
    "file_tools",
    "find_tool",
    "grep_tool",
    "ls_tool",
    "read_tool",
    "resolve_inside",
    "write_tool",
]
