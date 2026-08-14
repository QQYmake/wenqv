"""Standalone HTML template and print CSS for PDF exports."""

from __future__ import annotations

from html import escape


PDF_CSS = """
@page { size: A4; margin: 18mm 16mm 20mm; }
body {
  color: #1d2528;
  font-family: "LXGW WenKai Lite", sans-serif;
  font-size: 10.5pt;
  line-height: 1.72;
  overflow-wrap: anywhere;
}
h1, h2, h3, h4, h5, h6 { color: #123f43; line-height: 1.3; margin: 1.1em 0 0.45em; }
h1 { font-size: 22pt; }
h2 { font-size: 17pt; border-bottom: 0.5pt solid #b7c9c6; padding-bottom: 3pt; }
h3 { font-size: 14pt; }
p { margin: 0 0 0.75em; }
ul, ol { margin: 0 0 0.8em; padding-left: 1.7em; }
li { margin: 0.15em 0; }
blockquote { margin: 0.8em 0; padding: 0.15em 1em; border-left: 3pt solid #5b9d9c; color: #526467; }
pre { padding: 9pt 10pt; background: #eef3f0; border: 0.5pt solid #d0dfda; white-space: pre-wrap; font-family: "LXGW WenKai Lite", monospace; font-size: 8.5pt; }
code { font-family: "LXGW WenKai Lite", monospace; font-size: 0.92em; }
a { color: #145d73; text-decoration: underline; }
table { width: 100%; margin: 0.8em 0 1em; border-collapse: collapse; }
th, td { padding: 5pt 6pt; border: 0.5pt solid #b7c9c6; text-align: left; vertical-align: top; }
th { background: #e4efeb; font-weight: bold; }
hr { border: 0; border-top: 0.5pt solid #b7c9c6; margin: 1.2em 0; }
"""


def build_pdf_html(body_html: str, *, title: str = "Exported document") -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head>"
        "<meta charset=\"utf-8\"><title>"
        + escape(title)
        + "</title></head><body>"
        + body_html
        + "</body></html>"
    )


__all__ = ["PDF_CSS", "build_pdf_html"]
