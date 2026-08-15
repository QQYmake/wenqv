import type { ExportFileResult } from "../types";
import { getWorkspaceHeader, resolveApiUrl } from "./client";

const DOWNLOAD_URL_PATTERN = /^\/api\/files\/([a-f0-9]{32})$/;

export function parseExportFileResult(value: unknown): ExportFileResult | null {
  let candidate = value;
  if (typeof candidate === "string") {
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return null;
    }
  }
  if (!candidate || typeof candidate !== "object") return null;

  const result = candidate as Record<string, unknown>;
  if (result.error === true) return null;

  const filename = typeof result.filename === "string" ? result.filename.trim() : "";
  const downloadUrl = typeof result.download_url === "string" ? result.download_url : "";
  const match = DOWNLOAD_URL_PATTERN.exec(downloadUrl);
  if (!filename || !match) return null;

  const fileId = typeof result.file_id === "string" && result.file_id.trim()
    ? result.file_id.trim()
    : match[1];
  if (fileId !== match[1]) return null;

  return {
    file_id: fileId,
    filename,
    download_url: downloadUrl,
    mime_type: typeof result.mime_type === "string" ? result.mime_type : "",
  };
}

export async function triggerFileDownload(file: ExportFileResult): Promise<void> {
  const response = await fetch(resolveApiUrl(file.download_url), {
    credentials: "omit",
    headers: getWorkspaceHeader(),
  });
  if (!response.ok) throw new Error(`下载失败（${response.status}）`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
