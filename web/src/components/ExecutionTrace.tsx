import { useState } from "react";
import { resolveApiUrl } from "../api/client";
import type { ExportFileResult, ToolTrace } from "../types";
import { Icon } from "./Icon";

function formatPayload(value: unknown) {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function exportFileResult(value: unknown): ExportFileResult | null {
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
  const filename = typeof result.filename === "string" ? result.filename : "";
  const downloadUrl = typeof result.download_url === "string" ? result.download_url : "";
  const mimeType = typeof result.mime_type === "string" ? result.mime_type : "";
  if (!filename || !mimeType || !/^\/api\/files\/[a-f0-9]{32}$/.test(downloadUrl)) return null;
  return { filename, download_url: downloadUrl, mime_type: mimeType };
}

function ExportDownload({ result }: { result: unknown }) {
  const file = exportFileResult(result);
  if (!file) return null;
  return (
    <div className="trace-download" role="group" aria-label={`导出文件 ${file.filename}`}>
      <span className="trace-download__name">{file.filename}</span>
      <a
        className="trace-download__button"
        href={resolveApiUrl(file.download_url)}
        download={file.filename}
        aria-label={`下载 ${file.filename}`}
      >
        <Icon name="arrow-down" />
        <span>下载</span>
      </a>
    </div>
  );
}

function TraceItem({ trace }: { trace: ToolTrace }) {
  const [open, setOpen] = useState(trace.status === "running");
  return (
    <>
      <details
        className={`trace trace--${trace.status}`}
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          <span className="trace-state" aria-hidden="true" />
          <span className="trace-label">
            <small>
              {trace.status === "running"
                ? "正在调用"
                : trace.error
                  ? "调用出错"
                  : "调用完成"}
            </small>
            <strong>{trace.name}</strong>
          </span>
          {trace.truncated && <span className="trace-badge">已截断</span>}
          <Icon className="trace-chevron" name="chevron" />
        </summary>
        <div className="trace-body">
          {trace.arguments !== undefined && (
            <section>
              <h4>输入</h4>
              <pre>{formatPayload(trace.arguments)}</pre>
            </section>
          )}
          {trace.status !== "running" && (
            <section>
              <h4>{trace.error ? "错误" : "结果"}</h4>
              <pre>{formatPayload(trace.result)}</pre>
            </section>
          )}
          {trace.patch && (
            <section>
              <h4>{`修改差异${trace.patchTruncated ? "（已截断）" : ""}`}</h4>
              <pre className="trace-patch">{trace.patch}</pre>
            </section>
          )}
        </div>
      </details>
      {trace.name === "export_file" && trace.status === "success" && (
        <ExportDownload result={trace.result} />
      )}
    </>
  );
}

export function ExecutionTrace({ traces }: { traces: ToolTrace[] }) {
  if (traces.length === 0) return null;

  return (
    <div className="execution-traces" aria-label="执行轨迹">
      {traces.map((trace) => (
        <TraceItem key={trace.callId} trace={trace} />
      ))}
    </div>
  );
}
