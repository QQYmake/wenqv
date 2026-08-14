"""Shared Markdown parser and plain-text rendering helpers."""

from __future__ import annotations

import re
from typing import Any


_TOP_LEVEL_LIST_ITEM = re.compile(
    r"^[ ]{0,3}(?:(?:[-+*])(?:[ \t]+|$)|(?:\d{1,9}[.)])(?:[ \t]+|$))"
)
_ATX_HEADING = re.compile(r"^[ ]{0,3}#{1,6}(?:[ \t]+|$)")
_BLOCKQUOTE = re.compile(r"^[ ]{0,3}>")
_SETEXT_HEADING_UNDERLINE = re.compile(r"^[ ]{0,3}(?:=+|-+)[ \t]*$")
_THEMATIC_BREAK = re.compile(
    r"^[ ]{0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
_TABLE_DELIMITER = re.compile(
    r"^[ ]{0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$"
)
_REFERENCE_DEFINITION = re.compile(r"^[ ]{0,3}\[[^\]]+\]:[ \t]*\S")
_FENCE_OPENING = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")


def _line_content(line: str) -> str:
    return line.rstrip("\r\n")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _fence_opening(line: str) -> tuple[str, int] | None:
    match = _FENCE_OPENING.match(_line_content(line))
    if match is None:
        return None
    marker = match.group(1)
    # A backtick fence cannot have backticks in its info string. Matching that
    # rule here prevents an inline-code-looking line from masking later input.
    if marker[0] == "`" and "`" in match.group(2):
        return None
    return marker[0], len(marker)


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    marker, minimum_length = fence
    return bool(
        re.match(
            rf"^[ ]{{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*$",
            _line_content(line),
        )
    )


def _is_top_level_list_item(line: str) -> bool:
    value = _line_content(line)
    return not _THEMATIC_BREAK.match(value) and bool(_TOP_LEVEL_LIST_ITEM.match(value))


def _is_table_start(lines: list[str], index: int) -> bool:
    if "|" not in _line_content(lines[index]) or index + 1 >= len(lines):
        return False
    return bool(_TABLE_DELIMITER.match(_line_content(lines[index + 1])))


def _starts_nonparagraph_block(lines: list[str], index: int) -> bool:
    value = _line_content(lines[index])
    if not value.strip() or value[:1].isspace():
        return True
    if _is_top_level_list_item(value):
        return True
    if (
        _ATX_HEADING.match(value)
        or _BLOCKQUOTE.match(value)
        or _THEMATIC_BREAK.match(value)
        or _fence_opening(value)
        or _REFERENCE_DEFINITION.match(value)
        or _is_table_start(lines, index)
    ):
        return True
    # A Setext heading starts with ordinary text, so its underline must be
    # considered before deciding that the first line begins a paragraph.
    return index + 1 < len(lines) and bool(
        _SETEXT_HEADING_UNDERLINE.match(_line_content(lines[index + 1]))
    )


def normalize_markdown_for_export(content: str) -> str:
    """Add a missing paragraph break after a top-level list item.

    Model output occasionally places an unindented sentence immediately after
    a list item. CommonMark treats that sentence as a continuation of the
    final ``<li>``. For document export, unindented text at this narrow
    boundary means a new paragraph; list continuations must be explicitly
    indented. Nested lists, indented content, and recognised block starts are
    deliberately left untouched.
    """

    lines = content.splitlines(keepends=True)
    if len(lines) < 2:
        return content

    normalized: list[str] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        if (
            fence is None
            and index > 0
            and _is_top_level_list_item(lines[index - 1])
            and not _starts_nonparagraph_block(lines, index)
        ):
            # Preserve the source's newline convention whenever it is known.
            normalized.append(_line_ending(lines[index - 1]) or _line_ending(line) or "\n")
        normalized.append(line)

        if fence is None:
            fence = _fence_opening(line)
        elif _closes_fence(line, fence):
            fence = None

    return "".join(normalized)


def markdown_parser() -> Any:
    """Create the constrained GFM parser used by every export format.

    Raw HTML and images are disabled because exported content is model supplied.
    Linkify is also disabled so parsing never needs an optional network-oriented
    linkify dependency.
    """

    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("markdown-it-py is required for document export") from exc

    parser = MarkdownIt(
        "gfm-like",
        {"html": False, "linkify": False, "typographer": False},
    )
    parser.disable("image")
    return parser


def parse_markdown(content: str) -> list[Any]:
    return list(markdown_parser().parse(normalize_markdown_for_export(content)))


def render_markdown(content: str) -> str:
    """Render Markdown through the same export normalization as token parsing."""

    return markdown_parser().render(normalize_markdown_for_export(content))


def token_attrs(token: Any) -> dict[str, str]:
    attrs = token.attrs or {}
    if isinstance(attrs, dict):
        return {str(key): str(value) for key, value in attrs.items()}
    return {str(key): str(value) for key, value in attrs}


def inline_plain_text(token: Any, *, include_link_targets: bool = True) -> str:
    """Return visible text from one Markdown ``inline`` token."""

    children = token.children or ()
    output: list[str] = []
    links: list[str] = []
    for child in children:
        kind = child.type
        if kind in {"text", "code_inline", "html_inline"}:
            output.append(child.content)
        elif kind in {"softbreak", "hardbreak"}:
            output.append("\n")
        elif kind == "image":
            alt = token_attrs(child).get("alt", child.content)
            if alt:
                output.append(alt)
        elif kind == "link_open":
            links.append(token_attrs(child).get("href", ""))
        elif kind == "link_close" and links:
            href = links.pop()
            if include_link_targets and href:
                output.append(f" ({href})")
        # strong/emphasis/strike open and close tokens only change styling.
    return "".join(output)


def _append_line(lines: list[str], value: str) -> None:
    value = value.rstrip()
    if value:
        lines.extend(value.splitlines() or [value])


def markdown_to_plain_text(content: str) -> str:
    """Render Markdown as readable text while retaining block structure."""

    tokens = parse_markdown(content)
    lines: list[str] = []
    current: list[str] = []
    list_stack: list[tuple[str, int]] = []
    blockquote_depth = 0
    table_rows: list[list[str]] | None = None
    table_row: list[str] | None = None
    table_cell: list[str] | None = None

    def finish() -> None:
        nonlocal current
        value = "".join(current)
        current = []
        if value.strip():
            _append_line(lines, value)
            lines.append("")

    for token in tokens:
        kind = token.type
        if kind == "table_open":
            finish()
            table_rows = []
            continue
        if table_rows is not None:
            if kind == "tr_open":
                table_row = []
            elif kind in {"th_open", "td_open"}:
                table_cell = []
            elif kind == "inline" and table_cell is not None:
                table_cell.append(inline_plain_text(token))
            elif kind in {"th_close", "td_close"} and table_row is not None:
                table_row.append("".join(table_cell or []).strip())
                table_cell = None
            elif kind == "tr_close" and table_row is not None:
                table_rows.append(table_row)
                table_row = None
            elif kind == "table_close":
                for row in table_rows:
                    _append_line(lines, " | ".join(row))
                lines.append("")
                table_rows = None
            continue

        if kind == "blockquote_open":
            blockquote_depth += 1
        elif kind == "blockquote_close":
            finish()
            blockquote_depth = max(0, blockquote_depth - 1)
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            attrs = token_attrs(token)
            start = int(attrs.get("start", "1"))
            list_stack.append(("ordered" if kind.startswith("ordered") else "bullet", start - 1))
        elif kind == "list_item_open":
            list_kind, number = list_stack[-1] if list_stack else ("bullet", 0)
            if list_stack and list_kind == "ordered":
                number += 1
                list_stack[-1] = (list_kind, number)
            prefix = "  " * max(0, len(list_stack) - 1)
            prefix += f"{number}. " if list_kind == "ordered" else "- "
            current.append(prefix)
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            finish()
            if list_stack:
                list_stack.pop()
        elif kind in {"paragraph_open", "heading_open"}:
            if not current and blockquote_depth:
                current.append("> " * blockquote_depth)
        elif kind in {"paragraph_close", "heading_close"}:
            finish()
        elif kind == "inline":
            current.append(inline_plain_text(token))
        elif kind in {"fence", "code_block"}:
            finish()
            code_lines = token.content.rstrip("\n").splitlines()
            for code_line in code_lines or [""]:
                _append_line(lines, code_line)
            lines.append("")
        elif kind == "hr":
            finish()
            lines.append("---")

    finish()
    while lines and not lines[-1]:
        lines.pop()
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


__all__ = [
    "inline_plain_text",
    "markdown_parser",
    "markdown_to_plain_text",
    "normalize_markdown_for_export",
    "parse_markdown",
    "render_markdown",
    "token_attrs",
]
