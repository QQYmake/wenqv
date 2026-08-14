"""Markdown -> HTML -> PDF conversion using WeasyPrint."""

from __future__ import annotations

from pathlib import Path

from .errors import ExportConversionError
from .markdown_ast import markdown_parser
from .pdf_template import PDF_CSS, build_pdf_html


def markdown_to_html(content: str) -> str:
    return markdown_parser().render(content)


def _font_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "public" / "fonts" / "lxgw-wenkai-lite"


def markdown_to_pdf(content: str) -> bytes:
    try:
        from weasyprint import CSS, HTML
    except (ImportError, OSError) as exc:  # pragma: no cover - dependency guard
        raise ExportConversionError(
            "pdf_dependency_missing",
            "WeasyPrint and its native text-rendering libraries are required for PDF export",
        ) from exc

    font_directory = _font_directory()
    font_css_path = font_directory / "lxgwwenkailite-regular.css"
    if not font_css_path.is_file():
        raise ExportConversionError(
            "pdf_font_missing", "The bundled Chinese font assets are unavailable"
        )
    try:
        font_css = CSS(
            string=font_css_path.read_text(encoding="utf-8"),
            base_url=str(font_directory),
        )
        html = HTML(
            string=build_pdf_html(markdown_to_html(content)),
            base_url=str(Path(__file__).resolve().parents[3]),
        )
        return html.write_pdf(
            stylesheets=[CSS(string=PDF_CSS), font_css],
        )
    except ExportConversionError:
        raise
    except Exception as exc:
        raise ExportConversionError("pdf_conversion_failed", str(exc) or "PDF conversion failed") from exc


__all__ = ["markdown_to_html", "markdown_to_pdf"]
