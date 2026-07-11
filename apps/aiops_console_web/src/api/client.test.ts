import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, getActor } from "./client"

describe("API client request IDs", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("normalizes Gateway errors when randomUUID is unavailable on remote HTTP", async () => {
    vi.stubGlobal("crypto", {})
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      request_id: "req-gateway",
      error: {code: "unauthorized", message: "authentication required"},
    }), {status: 401, headers: {"Content-Type": "application/json"}})))

    await expect(getActor()).rejects.toEqual(new ApiError(401, "unauthorized", "authentication required", "req-gateway"))
  })
})
