import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

describe("api client cookie identity", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends the workspace cookie on JSON API requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ workspace_id: "ws-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const body = await api.bootstrap();
    expect(body.workspace_id).toBe("ws-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/bootstrap");
    // The identity is a cookie; without credentials the browser would never
    // send it, so every request must opt in explicitly (matters when
    // VITE_API_BASE_URL points at another origin).
    expect(init.credentials).toBe("include");
  });

  it("sends the workspace cookie on the chat SSE stream", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
        controller.close();
      },
    });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: stream });
    vi.stubGlobal("fetch", fetchMock);

    const events: Array<{ type: string }> = [];
    for await (const event of api.streamChat(
      { session_id: "s1", message: "hi" },
      new AbortController().signal,
    )) {
      events.push(event);
    }
    expect(events).toEqual([{ type: "done" }]);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chat");
    expect(init.credentials).toBe("include");
  });
});
