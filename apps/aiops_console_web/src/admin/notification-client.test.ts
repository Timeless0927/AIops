import { afterEach, describe, expect, it, vi } from "vitest"

import { createNotificationDestination } from "./notification-client"

describe("Notification client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("reuses a supplied credential request ID for reconciliation", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-notification"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "notification-unknown"}), {status: 201}))
    vi.stubGlobal("fetch", fetch)

    await createNotificationDestination({
      name: "Pilot Feishu",
      provider: "feishu",
      config: {webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/token"},
      reason: "configure Pilot notifications",
    }, "notification-unknown")

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/admin/notification-destinations", expect.objectContaining({
      headers: expect.objectContaining({"X-Request-ID": "notification-unknown"}),
    }))
  })
})
