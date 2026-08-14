"""Markdown -> HTML -> PDF conversion using WeasyPrint."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import ExportConversionError
from .markdown_ast import render_markdown
from .pdf_template import PDF_CSS, build_pdf_html


PDF_FONT_CSS_FILENAME = "lxgwwenkailite-regular-pdf.css"
PDF_FONT_FILENAME = "files/lxgwwenkailite-regular-full.woff2"


def markdown_to_html(content: str) -> str:
    return render_markdown(content)


def _font_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "public" / "fonts" / "lxgw-wenkai-lite"


def _pdf_font_assets(font_directory: Path) -> tuple[Path, Path]:
    """Return the PDF-only CSS and complete WOFF2, or a stable asset error."""

    font_css_path = font_directory / PDF_FONT_CSS_FILENAME
    font_path = font_directory / PDF_FONT_FILENAME
    if not font_css_path.is_file() or not font_path.is_file():
        raise ExportConversionError(
            "pdf_font_missing", "The bundled PDF Chinese font assets are unavailable"
        )
    return font_css_path, font_path


def _font_only_url_fetcher(font_directory: Path, url_fetcher_type: object, fatal_error_type: object) -> object:
    """Return a WeasyPrint fetcher that can only read bundled font files.

    Markdown HTML is model-supplied. We intentionally do not delegate arbitrary
    URLs to WeasyPrint's default fetcher because it can read local ``file://``
    resources and make HTTP requests. The renderer only needs the ``.woff2``
    files referenced by the checked-in PDF-only LXGW CSS.
    """

    class FontOnlyURLFetcher(url_fetcher_type):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__(
                timeout=5,
                allowed_protocols=("file",),
                allow_redirects=False,
            )

        def fetch(self, url: str, headers: object = None) -> object:
            parsed = urlparse(url)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                raise fatal_error_type(
                    "PDF export only permits local bundled font resources"
                )
            raw_path = unquote(parsed.path)
            if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
                raw_path = raw_path[1:]
            candidate = Path(raw_path).resolve()
            try:
                candidate.relative_to(font_directory)
            except ValueError as exc:
                raise fatal_error_type(
                    "PDF export attempted to read a file outside the bundled font directory"
                ) from exc
            if candidate.suffix.lower() != ".woff2" or not candidate.is_file():
                raise fatal_error_type("PDF export only permits existing .woff2 font files")
            return super().fetch(url, headers=headers)

    return FontOnlyURLFetcher()


def markdown_to_pdf(content: str) -> bytes:
    font_directory = _font_directory()
    font_css_path, _font_path = _pdf_font_assets(font_directory)
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
        from weasyprint.urls import FatalURLFetchingError, URLFetcher
    except (ImportError, OSError) as exc:  # pragma: no cover - dependency guard
        raise ExportConversionError(
            "pdf_dependency_missing",
            "WeasyPrint and its native text-rendering libraries are required for PDF export",
        ) from exc

    try:
        font_config = FontConfiguration()
        url_fetcher = _font_only_url_fetcher(
            font_directory, URLFetcher, FatalURLFetchingError
        )
        font_css = CSS(
            string=font_css_path.read_text(encoding="utf-8"),
            base_url=str(font_directory),
            url_fetcher=url_fetcher,
            font_config=font_config,
        )
        html = HTML(
            string=build_pdf_html(markdown_to_html(content)),
            base_url=str(font_directory),
            url_fetcher=url_fetcher,
        )
        return html.write_pdf(
            stylesheets=[CSS(string=PDF_CSS), font_css],
            font_config=font_config,
        )
    except ExportConversionError:
        raise
    except Exception as exc:
        raise ExportConversionError("pdf_conversion_failed", str(exc) or "PDF conversion failed") from exc


__all__ = ["markdown_to_html", "markdown_to_pdf"]
