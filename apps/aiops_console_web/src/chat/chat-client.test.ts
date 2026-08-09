import { afterEach, describe, expect, it, vi } from "vitest"

import { cancelChatMessage, createChatHandoff, retryChatMessage, sendChatMessage } from "./chat-client"

describe("Chat client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("sends and retries Chat messages through creator-scoped CSRF routes", async () => {
    const chat = {id: "chat-1", messages: []}
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-chat"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-chat", chat_session: chat})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-retry"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-retry", chat_session: chat})))
    vi.stubGlobal("fetch", fetch)

    await sendChatMessage("chat/1", "解释 Deployment", "message-1")
    await retryChatMessage("chat/1", "message/1")

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/chat/sessions/chat%2F1/messages", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({"X-CSRF-Token": "csrf-chat"}),
      body: JSON.stringify({content: "解释 Deployment", idempotency_key: "message-1"}),
    }))
    expect(fetch).toHaveBeenNthCalledWith(4, "/api/v1/chat/sessions/chat%2F1/messages/message%2F1/retry", expect.objectContaining({
      method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-retry"}),
    }))
  })

  it("cancels the current Chat response through its CSRF route", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-cancel"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-cancel", chat_session: {id: "chat-1", messages: []}})))
    vi.stubGlobal("fetch", fetch)

    await cancelChatMessage("chat/1", "cancel-1")

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/chat/sessions/chat%2F1/messages/cancel", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({"X-CSRF-Token": "csrf-cancel"}),
      body: JSON.stringify({idempotency_key: "cancel-1"}),
    }))
  })

  it("sends an explicit environment scope with a Chat message", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-chat"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-chat", chat_session: {id: "chat-1", messages: []}})))
    vi.stubGlobal("fetch", fetch)

    await sendChatMessage("chat/1", "检查错误率", "message-2", {
      cluster_id: "cluster-prod",
      deployment_target_id: "target-checkout",
    })

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/chat/sessions/chat%2F1/messages", expect.objectContaining({
      body: JSON.stringify({
        content: "检查错误率",
        idempotency_key: "message-2",
        scope: {cluster_id: "cluster-prod", deployment_target_id: "target-checkout"},
      }),
    }))
  })

  it("submits an explicit Chat Handoff through the creator-scoped CSRF route", async () => {
    const handoff = {id: "handoff-1", incident_id: "incident-1", idempotent: false}
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-handoff"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-handoff", handoff})))
    vi.stubGlobal("fetch", fetch)

    expect(await createChatHandoff("chat/1", ["message/1"], {type: "existing_incident", incident_id: "incident/1"}, "handoff-1")).toEqual(handoff)

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/chat/sessions/chat%2F1/handoffs", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({"X-CSRF-Token": "csrf-handoff"}),
      body: JSON.stringify({
        message_ids: ["message/1"], idempotency_key: "handoff-1",
        target: {type: "existing_incident", incident_id: "incident/1"},
      }),
    }))
  })
})
