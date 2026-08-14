"""Shared Markdown parser and plain-text rendering helpers."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


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
    return list(markdown_parser().parse(content))


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
    "parse_markdown",
    "token_attrs",
]
