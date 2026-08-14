import { useState } from "react";
import type { ToolTrace } from "../types";
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

function TraceItem({ trace }: { trace: ToolTrace }) {
  const [open, setOpen] = useState(trace.status === "running");
  return (
    <details
      className={`trace trace--${trace.status}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="trace-state" aria-hidden="true" />
        <span className="trace-label">
          <small>{trace.status === "running" ? "正在调用" : trace.error ? "调用出错" : "调用完成"}</small>
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
            <h4>修改差异{trace.patchTruncated ? "（已截断）" : ""}</h4>
            <pre className="trace-patch">{trace.patch}</pre>
          </section>
        )}
      </div>
    </details>
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
