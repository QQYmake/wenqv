#!/usr/bin/env python3
"""Generate and download all export formats through the deployed HTTP API.

Run this on Ubuntu with the application virtualenv after nginx and HTTPS are
ready, for example:

    sudo -u bluelake-agent /opt/bluelake-agent/.venv/bin/python \
      /opt/bluelake-agent/deploy/pdf_smoke_test.py \
      --base-url https://agent.example.com
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from server.services.document_exporter import DocumentExporter, ExportedFile


SMOKE_MARKDOWN = """# 中文 PDF 导出 Smoke Test

这是 Ubuntu 生产环境的中文正文，包含 **粗体** 与 *斜体*，用于验证字体和排版。

- 无序列表项目
- 第二个列表项目

1. 有序列表项目
2. 第二个有序项目

```python
print("你好，Ubuntu")
```

| 项目 | 状态 |
| --- | --- |
| 中文字体 | 正常 |
| 表格 | 42 |

这是一个[打开链接](https://example.com)的测试。
"""

EXPORT_MIME_TYPES = {
    "md": "text/markdown",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public HTTPS origin")
    parser.add_argument(
        "--workspace-root",
        default="/var/lib/bluelake-agent/workspaces",
        help="The same AGENT_WORKSPACE_ROOT used by systemd",
    )
    return parser.parse_args()


def _run_text_command(command: list[str], pdf_path: Path) -> str:
    arguments = [*command, str(pdf_path)]
    if command[0] == "pdftotext":
        # Without an explicit output path pdftotext writes beside the input
        # file, leaving stdout empty and making the text assertions ineffective.
        arguments.append("-")
    try:
        completed = subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{command[0]} is required; install poppler-utils before running this smoke test"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}") from exc
    return completed.stdout


def _check_pdf(pdf_path: Path) -> None:
    data = pdf_path.read_bytes()
    if len(data) == 0 or not data.startswith(b"%PDF"):
        raise RuntimeError("downloaded file is not a non-empty PDF")

    extracted = _run_text_command(["pdftotext"], pdf_path)
    for marker in (
        "中文 PDF 导出 Smoke Test",
        "中文正文",
        "粗体",
        "斜体",
        "列表项目",
        "第二个列表项目",
        "print",
        "项目",
        "状态",
        "表格",
        "打开链接",
    ):
        if marker not in extracted:
            raise RuntimeError(f"PDF text check did not find marker: {marker}")

    font_output = _run_text_command(["pdffonts"], pdf_path)
    font_rows = [
        line
        for line in font_output.splitlines()
        if line.strip() and not line.startswith("name") and not line.startswith("----")
    ]
    if not font_rows or not any("yes" in line.lower().split() for line in font_rows):
        raise RuntimeError("PDF contains no embedded fonts")

    info_output = _run_text_command(["pdfinfo"], pdf_path)
    page_lines = [line for line in info_output.splitlines() if line.startswith("Pages:")]
    if not page_lines or int(page_lines[0].split(":", 1)[1].strip()) < 1:
        raise RuntimeError("PDF page-count check failed")


def _check_download(exported: ExportedFile, response: httpx.Response) -> None:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith(EXPORT_MIME_TYPES[exported.format]):
        raise RuntimeError(
            f"{exported.format} Content-Type mismatch: {content_type!r}"
        )
    disposition = response.headers.get("content-disposition", "")
    if "attachment" not in disposition.lower():
        raise RuntimeError(f"{exported.format} response is not an attachment")
    if exported.filename not in disposition and "filename*=" not in disposition:
        raise RuntimeError(f"{exported.format} filename is missing from Content-Disposition")

    data = response.content
    if not data:
        raise RuntimeError(f"{exported.format} download is empty")
    if exported.format == "md" and data != SMOKE_MARKDOWN.encode("utf-8"):
        raise RuntimeError("Markdown download content changed")
    if exported.format == "txt":
        text = data.decode("utf-8")
        if "中文 PDF 导出 Smoke Test" not in text or "粗体" not in text:
            raise RuntimeError("text download is missing smoke markers")
    elif exported.format == "docx" and not data.startswith(b"PK"):
        raise RuntimeError("DOCX download is not a ZIP package")
    elif exported.format == "pdf":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            pdf_path = Path(handle.name)
            handle.write(data)
        try:
            _check_pdf(pdf_path)
        finally:
            pdf_path.unlink(missing_ok=True)


def main() -> int:
    args = _parse_args()
    exporter = DocumentExporter()
    workspace_root = Path(args.workspace_root).resolve()
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        bootstrap = client.get("/api/bootstrap")
        bootstrap.raise_for_status()
        workspace_id = str(bootstrap.json()["workspace_id"])

        for format_name in ("md", "txt", "docx", "pdf"):
            exported = exporter.export(
                filename=f"production-{format_name}-smoke",
                format=format_name,
                content=SMOKE_MARKDOWN,
                workspace_root=workspace_root / workspace_id,
            )
            try:
                response = client.get(exported.download_url)
                _check_download(exported, response)
            finally:
                exported.path.unlink(missing_ok=True)
                exported.path.with_suffix(".json").unlink(missing_ok=True)

    print("export smoke test passed: generated and downloaded md, txt, docx, and pdf")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"export smoke test failed: {exc}") from exc
