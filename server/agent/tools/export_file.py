"""Agent Tool for exporting Markdown into downloadable documents."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ...services.document_exporter import DocumentExportError, DocumentExporter
from ..registry import Tool, ToolExecutionContext


def export_file_tool(exporter: DocumentExporter | None = None) -> Tool:
    document_exporter = exporter or DocumentExporter()

    async def execute(arguments: Mapping[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        try:
            exported = await asyncio.to_thread(
                document_exporter.export,
                filename=str(arguments["filename"]),
                format=str(arguments["format"]),
                content=str(arguments["content"]),
                workspace_root=context.workspace_root,
            )
        except DocumentExportError as exc:
            return exc.as_dict()
        except Exception as exc:  # pragma: no cover - final safety boundary
            return {
                "error": True,
                "code": "export_failed",
                "message": str(exc) or exc.__class__.__name__,
            }
        return exported.to_result()

    return Tool(
        name="export_file",
        description=(
            "Export valid Markdown content as a downloadable md, txt, docx, or pdf file. "
            "Always pass the document body as valid Markdown; the server performs conversion. "
            "When a list ends before an independent paragraph, leave a blank line between them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Base filename without an extension.",
                },
                "format": {
                    "type": "string",
                    "enum": ["md", "txt", "docx", "pdf"],
                    "description": "Output format.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown document content.",
                },
            },
            "required": ["filename", "format", "content"],
            "additionalProperties": False,
        },
        executor=execute,
    )


__all__ = ["export_file_tool"]
