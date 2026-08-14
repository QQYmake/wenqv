"""Plain-text export."""

from __future__ import annotations

from .markdown_ast import markdown_to_plain_text


def markdown_to_txt(content: str) -> bytes:
    return markdown_to_plain_text(content).encode("utf-8")


__all__ = ["markdown_to_txt"]
