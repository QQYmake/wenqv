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

  it("decodes multibyte UTF-8 split across chunk boundaries", async () => {
    // Hand-slice the encoded bytes so 湖 and 畔 are each split mid-character.
    const encoder = new TextEncoder();
    const prefix = encoder.encode('event: text_delta\ndata: {"delta":"');
    const suffix = encoder.encode('"}\n\n');
    const hu = encoder.encode("湖"); // 3 bytes
    const pan = encoder.encode("畔"); // 3 bytes
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(prefix);
        controller.enqueue(hu.slice(0, 2)); // first two bytes of 湖
        controller.enqueue(new Uint8Array([hu[2], pan[0]])); // 湖 tail + 畔 head
        controller.enqueue(pan.slice(1)); // rest of 畔
        controller.enqueue(suffix);
        controller.close();
      },
    });

    const events = [];
    for await (const event of parseSSEStream(stream)) events.push(event);
    expect(events).toEqual([{ event: "text_delta", data: '{"delta":"湖畔"}' }]);
  });

  it("handles CR-only line endings and defaults to the message event", async () => {
    // \r alone is a valid SSE line terminator; two data lines in one event.
    const events = await collect(["data: hello\rdata: world\r"]);
    expect(events).toEqual([{ event: "message", data: "hello\nworld" }]);
  });
});
