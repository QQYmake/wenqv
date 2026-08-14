"""Markdown export."""

from __future__ import annotations


def markdown_to_md(content: str) -> bytes:
    return content.encode("utf-8")


__all__ = ["markdown_to_md"]
