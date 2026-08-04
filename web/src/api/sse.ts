export interface SSEMessage {
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

/**
 * Incrementally parses a Server-Sent Events byte stream. It deliberately keeps
 * transport parsing separate from JSON/event semantics so it is easy to test
 * fragmented UTF-8 chunks and harmless fields added by the server later.
 */
export async function* parseSSEStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SSEMessage> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "message";
  let eventId: string | undefined;
  let retry: number | undefined;
  let dataLines: string[] = [];

  const dispatch = (): SSEMessage | undefined => {
    if (dataLines.length === 0) return undefined;
    const message: SSEMessage = {
      event: eventName || "message",
      data: dataLines.join("\n"),
    };
    if (eventId !== undefined) message.id = eventId;
    if (retry !== undefined) message.retry = retry;
    eventName = "message";
    dataLines = [];
    retry = undefined;
    return message;
  };

  const consumeLine = (line: string): SSEMessage | undefined => {
    if (line === "") return dispatch();
    if (line.startsWith(":")) return undefined;

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    switch (field) {
      case "event":
        eventName = value;
        break;
      case "data":
        dataLines.push(value);
        break;
      case "id":
        if (!value.includes("\0")) eventId = value;
        break;
      case "retry": {
        const parsed = Number.parseInt(value, 10);
        if (Number.isFinite(parsed)) retry = parsed;
        break;
      }
    }
    return undefined;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

      let newline = buffer.indexOf("\n");
      while (newline !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        const message = consumeLine(line);
        if (message) yield message;
        newline = buffer.indexOf("\n");
      }

      if (done) break;
    }

    if (buffer.length > 0) consumeLine(buffer);
    const finalMessage = dispatch();
    if (finalMessage) yield finalMessage;
  } finally {
    reader.releaseLock();
  }
}

export function streamFromStrings(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
}
