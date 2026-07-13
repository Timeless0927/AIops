import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, createChangeRequest, getActor, submitChangeRequestInput } from "./client"

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

  it("submits Change Request writes through the shared CSRF client", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-1"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", change_request: {id: "change-1"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-2"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-2", change_request: {id: "change-1"}})))
    vi.stubGlobal("fetch", fetch)

    await createChangeRequest("incident/1", {
      desired_outcome: "恢复服务",
      context: "发布后异常",
      idempotency_key: "create-1",
    })
    await submitChangeRequestInput("change/1", {content: "gateway-v41", idempotency_key: "input-1"})

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/incidents/incident%2F1/change-requests",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-1"})}),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/api/v1/change-requests/change%2F1/input",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-2"})}),
    )
  })
})
