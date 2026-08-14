from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.agent.memory import InMemoryConversationStore
from server.agent.registry import ToolExecutionContext
from server.agent.tools import export_file_tool
from server.services.document_exporter import (
    MAX_MARKDOWN_BYTES,
    DocumentExportError,
    DocumentExporter,
    sanitize_filename,
)
from server.services.exporters.markdown_ast import markdown_to_plain_text
from server.services.exporters.pdf_exporter import _font_only_url_fetcher


MARKDOWN_FIXTURE = """# 导出报告

这是一个**粗体**和*斜体*段落，包含[链接](https://example.com)。

- 无序项目
- 第二项

1. 有序项目
2. 第二项

> 这是一段引用。

```python
print('你好')
```

| 名称 | 值 |
| --- | --- |
| 中文 | 42 |
"""


def test_markdown_to_plain_text_removes_markup_but_keeps_blocks() -> None:
    rendered = markdown_to_plain_text(MARKDOWN_FIXTURE)

    assert "# " not in rendered
    assert "**" not in rendered
    assert "导出报告" in rendered
    assert "- 无序项目" in rendered
    assert "1. 有序项目" in rendered
    assert "print('你好')" in rendered
    assert "中文 | 42" in rendered
    assert "https://example.com" in rendered


def test_exporter_writes_all_formats_and_docx_structure(tmp_path: Path) -> None:
    exporter = DocumentExporter()
    outputs = {}
    pdf_available = True
    for format_name in ("md", "txt", "docx", "pdf"):
        try:
            outputs[format_name] = exporter.export(
                filename="report",
                format=format_name,
                content=MARKDOWN_FIXTURE,
                workspace_root=tmp_path,
            )
        except DocumentExportError as exc:
            if format_name == "pdf" and exc.code == "pdf_dependency_missing":
                pdf_available = False
                continue
            raise

    assert outputs["md"].path.read_text(encoding="utf-8") == MARKDOWN_FIXTURE
    assert "**粗体**" not in outputs["txt"].path.read_text(encoding="utf-8")
    assert outputs["md"].mime_type.startswith("text/markdown")
    assert outputs["txt"].filename == "report.txt"
    assert outputs["docx"].path.read_bytes().startswith(b"PK")
    if pdf_available:
        assert outputs["pdf"].path.read_bytes().startswith(b"%PDF")

    from docx import Document

    document = Document(outputs["docx"].path)
    paragraph_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "导出报告" in paragraph_text
    assert "无序项目" in paragraph_text
    assert "粗体" in paragraph_text
    assert any(
        paragraph.style.name == "List Bullet"
        for paragraph in document.paragraphs
    )
    assert any(
        paragraph.style.name == "List Number"
        for paragraph in document.paragraphs
    )
    assert document.tables[0].cell(1, 0).text == "中文"
    assert document.tables[0].cell(1, 1).text == "42"


def test_export_ids_are_unique_and_filename_cannot_escape_workspace(tmp_path: Path) -> None:
    exporter = DocumentExporter()
    first = exporter.export(
        filename="../outside/../report",
        format="md",
        content="first",
        workspace_root=tmp_path,
    )
    second = exporter.export(
        filename="../outside/../report",
        format="md",
        content="second",
        workspace_root=tmp_path,
    )

    export_dir = tmp_path / ".agent-exports"
    assert first.file_id != second.file_id
    assert first.path.parent == export_dir
    assert second.path.parent == export_dir
    assert first.filename == second.filename
    assert not (tmp_path.parent / "outside").exists()
    assert exporter.resolve(first.file_id, tmp_path) == first


def test_export_limits_and_structured_tool_errors(tmp_path: Path) -> None:
    exporter = DocumentExporter()
    with pytest.raises(DocumentExportError, match="exceeds"):
        exporter.export(
            filename="too-large",
            format="md",
            content="x" * (MAX_MARKDOWN_BYTES + 1),
            workspace_root=tmp_path,
        )

    context = ToolExecutionContext(
        session_id="session",
        store=InMemoryConversationStore(),
        workspace_root=tmp_path,
        request_id="request",
    )
    tool = export_file_tool(exporter)
    error = asyncio.run(
        tool.executor(
            {"filename": "", "format": "pdf", "content": "# title"},
            context,
        )
    )
    assert isinstance(error, dict)
    assert error["error"] is True
    assert error["code"] == "invalid_filename"


def test_sanitize_filename_removes_unsafe_segments() -> None:
    safe = sanitize_filename("..\\..\\CON?.pdf", "pdf")
    assert "/" not in safe and "\\" not in safe
    assert ".." not in safe
    assert safe != "CON"


def test_pdf_url_fetcher_only_allows_bundled_woff2_files(tmp_path: Path) -> None:
    class FakeURLFetcher:
        def __init__(self, **kwargs) -> None:
            self.options = kwargs

        def fetch(self, url: str, headers=None):
            return {"url": url}

    class FakeFatalURLFetchingError(RuntimeError):
        pass

    font_directory = tmp_path / "fonts"
    font_directory.mkdir()
    bundled_font = font_directory / "files" / "font.woff2"
    bundled_font.parent.mkdir()
    bundled_font.write_bytes(b"font")
    outside_font = tmp_path / "outside.woff2"
    outside_font.write_bytes(b"outside")

    fetcher = _font_only_url_fetcher(
        font_directory,
        FakeURLFetcher,
        FakeFatalURLFetchingError,
    )

    assert fetcher.fetch(bundled_font.as_uri())["url"] == bundled_font.as_uri()
    with pytest.raises(FakeFatalURLFetchingError):
        fetcher.fetch(outside_font.as_uri())
    with pytest.raises(FakeFatalURLFetchingError):
        fetcher.fetch("https://example.com/font.woff2")
