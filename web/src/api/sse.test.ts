import { describe, expect, it } from "vitest";
import { parseSSEStream, streamFromStrings } from "./sse";

async function collect(chunks: string[]) {
  const events = [];
  for await (const event of parseSSEStream(streamFromStrings(chunks))) events.push(event);
  return events;
}

describe("parseSSEStream", () => {
  it("parses named events split across arbitrary byte chunks", async () => {
    const events = await collect([
      "event: text_",
      "delta\r\ndata: {\"type\":\"text_delta\",",
      "\"delta\":\"湖\"}\r\n\r\nevent: done\ndata: {\"type\":\"done\"}\n\n",
    ]);

    expect(events).toEqual([
      {
        event: "text_delta",
        data: '{"type":"text_delta","delta":"湖"}',
      },
      { event: "done", data: '{"type":"done"}' },
    ]);
  });

  it("joins multiline data, ignores comments, and flushes the last event", async () => {
    const events = await collect([
      ": heartbeat\n",
      "id: 42\nevent: tool_result\ndata: first\ndata: second",
    ]);

    expect(events).toEqual([
      { event: "tool_result", data: "first\nsecond", id: "42" },
    ]);
  });
});
