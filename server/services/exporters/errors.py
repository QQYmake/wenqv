"""Errors raised by optional document conversion backends."""

from __future__ import annotations


class ExportConversionError(RuntimeError):
    """A conversion failed in a way that can be returned to the tool caller."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


__all__ = ["ExportConversionError"]
