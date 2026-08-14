"""Document export orchestration, safe storage, and download metadata."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid
from typing import Any, Literal

from .exporters import markdown_to_docx, markdown_to_md, markdown_to_pdf, markdown_to_txt
from .exporters.errors import ExportConversionError


ExportFormat = Literal["md", "txt", "docx", "pdf"]

MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
MAX_EXPORTED_BYTES = 10 * 1024 * 1024
MAX_FILENAME_LENGTH = 120
EXPORT_DIRECTORY = ".agent-exports"
_FILE_ID = re.compile(r"^[0-9a-f]{32}$")
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SUPPORTED_FORMATS: dict[str, tuple[str, str, str]] = {
    "md": (".md", "text/markdown; charset=utf-8", "markdown_to_md"),
    "txt": (".txt", "text/plain; charset=utf-8", "markdown_to_txt"),
    "docx": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "markdown_to_docx",
    ),
    "pdf": (".pdf", "application/pdf", "markdown_to_pdf"),
}


class DocumentExportError(ValueError):
    """Expected export failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"error": True, "code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class ExportedFile:
    file_id: str
    filename: str
    format: ExportFormat
    mime_type: str
    path: Path

    @property
    def download_url(self) -> str:
        return f"/api/files/{self.file_id}"

    def to_result(self) -> dict[str, str]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "download_url": self.download_url,
            "mime_type": self.mime_type,
        }


def sanitize_filename(filename: str, file_format: str) -> str:
    value = unicodedata.normalize("NFKC", filename).strip()
    if not value:
        raise DocumentExportError("invalid_filename", "filename cannot be blank")
    # Treat path separators and traversal markers as ordinary unsafe input;
    # they are replaced before the result is ever used as a filesystem path.
    value = _INVALID_FILENAME.sub("_", value).replace("..", "_")
    for extension in (".md", ".txt", ".docx", ".pdf"):
        if value.casefold().endswith(extension):
            value = value[: -len(extension)]
            break
    value = value.strip(" .")[:MAX_FILENAME_LENGTH].rstrip(" .")
    if not value or value in {".", ".."}:
        raise DocumentExportError("invalid_filename", "filename does not contain a usable name")
    if value.upper().split(".", 1)[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        value = f"_{value}"
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DocumentExporter:
    """Convert Markdown and store an opaque, workspace-scoped download."""

    def __init__(
        self,
        *,
        max_markdown_bytes: int = MAX_MARKDOWN_BYTES,
        max_exported_bytes: int = MAX_EXPORTED_BYTES,
    ) -> None:
        self.max_markdown_bytes = max_markdown_bytes
        self.max_exported_bytes = max_exported_bytes

    def export(
        self,
        *,
        filename: str,
        format: str,
        content: str,
        workspace_root: str | Path,
    ) -> ExportedFile:
        if format not in _SUPPORTED_FORMATS:
            raise DocumentExportError(
                "unsupported_format", "format must be one of md, txt, docx, pdf"
            )
        if not isinstance(content, str):
            raise DocumentExportError("invalid_content", "content must be a Markdown string")
        content_size = len(content.encode("utf-8"))
        if content_size > self.max_markdown_bytes:
            raise DocumentExportError(
                "content_too_large",
                f"Markdown content exceeds the {self.max_markdown_bytes} byte limit",
            )
        safe_name = sanitize_filename(filename, format)
        extension, mime_type, exporter_name = _SUPPORTED_FORMATS[format]
        exporter = {
            "markdown_to_md": markdown_to_md,
            "markdown_to_txt": markdown_to_txt,
            "markdown_to_docx": markdown_to_docx,
            "markdown_to_pdf": markdown_to_pdf,
        }[exporter_name]
        try:
            data = exporter(content)
        except ExportConversionError as exc:
            raise DocumentExportError(exc.code, str(exc)) from exc
        except Exception as exc:
            raise DocumentExportError(
                "conversion_failed", str(exc) or "document conversion failed"
            ) from exc
        if len(data) > self.max_exported_bytes:
            raise DocumentExportError(
                "file_too_large",
                f"Generated file exceeds the {self.max_exported_bytes} byte limit",
            )

        root = Path(workspace_root).resolve()
        export_dir = (root / EXPORT_DIRECTORY).resolve()
        export_dir.mkdir(parents=True, exist_ok=True)
        export_dir.relative_to(root)
        file_id = uuid.uuid4().hex
        output_path = export_dir / f"{file_id}{extension}"
        manifest_path = export_dir / f"{file_id}.json"
        manifest = {
            "file_id": file_id,
            "filename": f"{safe_name}{extension}",
            "format": format,
            "mime_type": mime_type,
            "storage_name": output_path.name,
        }
        try:
            _atomic_write(output_path, data)
            _atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise DocumentExportError("storage_failed", str(exc) or "could not store exported file") from exc
        return ExportedFile(
            file_id=file_id,
            filename=manifest["filename"],
            format=format,  # type: ignore[arg-type]
            mime_type=mime_type,
            path=output_path,
        )

    def resolve(self, file_id: str, workspace_root: str | Path) -> ExportedFile | None:
        if not _FILE_ID.fullmatch(file_id):
            return None
        root = Path(workspace_root).resolve()
        export_dir = (root / EXPORT_DIRECTORY).resolve()
        try:
            export_dir.relative_to(root)
        except ValueError:
            return None
        manifest_path = export_dir / f"{file_id}.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("file_id") != file_id:
                return None
            file_format = str(manifest.get("format", ""))
            metadata = _SUPPORTED_FORMATS.get(file_format)
            if metadata is None:
                return None
            extension, mime_type, _ = metadata
            storage_name = str(manifest.get("storage_name", ""))
            expected_name = f"{file_id}{extension}"
            if storage_name != expected_name:
                return None
            path = (export_dir / storage_name).resolve()
            path.relative_to(export_dir)
            if not path.is_file():
                return None
            filename = str(manifest.get("filename", ""))
            if not filename.endswith(extension) or Path(filename).name != filename:
                return None
            return ExportedFile(
                file_id=file_id,
                filename=filename,
                format=file_format,  # type: ignore[arg-type]
                mime_type=str(manifest.get("mime_type") or mime_type),
                path=path,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


__all__ = [
    "EXPORT_DIRECTORY",
    "MAX_EXPORTED_BYTES",
    "MAX_FILENAME_LENGTH",
    "MAX_MARKDOWN_BYTES",
    "DocumentExportError",
    "DocumentExporter",
    "ExportFormat",
    "ExportedFile",
    "sanitize_filename",
]
