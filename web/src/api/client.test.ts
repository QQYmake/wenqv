import { afterEach, describe, expect, it, vi } from "vitest";
import { api, setWorkspaceId } from "./client";

describe("browser-local API transport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    setWorkspaceId("");
  });

  it("uses the local anonymous header and never sends cookies", async () => {
    setWorkspaceId("browser-uuid");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ features: {} }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.getConfig();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/config");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({ "X-Workspace-ID": "browser-uuid" });
  });

  it("sends browser-owned runtime state and temporary Provider config with chat", async () => {
    setWorkspaceId("browser-uuid");
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done"}\n\n'));
        controller.close();
      },
    });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: stream });
    vi.stubGlobal("fetch", fetchMock);
    const events = [];
    for await (const event of api.streamChat({
      session_id: "local-session",
      message: "hello",
      runtime_context: { messages: [], active_skills: [] },
      provider_config: {
        main: { base_url: "https://provider.example/v1", api_key: "secret", model: "model" },
        summary: { base_url: "", api_key: "", model: "" },
      },
      reasoning_effort: "medium",
    }, new AbortController().signal)) events.push(event);

    expect(events).toEqual([{ type: "done" }]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/chat");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toMatchObject({ "X-Workspace-ID": "browser-uuid" });
    expect(JSON.parse(String(init.body))).toMatchObject({
      session_id: "local-session",
      runtime_context: { messages: [], active_skills: [] },
      provider_config: { main: { api_key: "secret" } },
    });
  });
});
