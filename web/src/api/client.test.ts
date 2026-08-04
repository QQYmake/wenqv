import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

describe("api request error handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces a readable error instead of a SyntaxError for a 200 HTML body", async () => {
    // A stale backend can answer an API path with the SPA shell (200 + HTML);
    // response.json() would otherwise throw "Unexpected token '<', ...".
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><html><body>SPA</body></html>", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );

    const rejection = api.getConfig().catch((error: unknown) => error);
    const error = await rejection;
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toMatch(/非 JSON 响应/);
    expect(error).toMatchObject({ status: 200 });
  });

  it("uses the JSON detail for an error response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "API 未配置" }), {
          status: 412,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api.getConfig()).rejects.toThrow("API 未配置");
  });
});
