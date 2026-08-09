import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/transport"
import { getActor, logout } from "./auth-client"

describe("authentication client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("normalizes Gateway authentication errors", async () => {
    vi.stubGlobal("crypto", {})
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      request_id: "req-gateway",
      error: {code: "unauthorized", message: "authentication required"},
    }), {status: 401, headers: {"Content-Type": "application/json"}})))

    await expect(getActor()).rejects.toEqual(new ApiError(401, "unauthorized", "authentication required", "req-gateway"))
  })

  it("logs out through the empty CSRF POST contract", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-logout"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({})))
    vi.stubGlobal("fetch", fetch)

    await logout()

    expect(fetch).toHaveBeenNthCalledWith(2, "/auth/logout", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({"X-CSRF-Token": "csrf-logout"}),
      body: undefined,
    }))
  })
})
