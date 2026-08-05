import { afterEach, describe, expect, it, vi } from "vitest"

import {
  ApiError,
  acceptKubernetesReconciliation,
  approveKubernetesPhase,
  cancelKubernetesPhaseExecution,
  createChatHandoff,
  createChangeRequest,
  createNotificationDestination,
  createMCPIntegration,
  createKubernetesChangeAuthority,
  createSecureInput,
  getActor,
  getAdminAudit,
  getMCPIntegrations,
  newClientId,
  startKubernetesPhaseExecution,
  retryChangeRequestPlanning,
  retryChatMessage,
  updateMCPIntegration,
  verifyMCPIntegration,
  sendChatMessage,
  submitChangeRequestInput,
} from "./client"

describe("API client request IDs", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("generates client IDs when randomUUID is unavailable on remote HTTP", () => {
    vi.stubGlobal("crypto", {})

    expect(newClientId()).toMatch(/^req-[a-z0-9]+-[a-z0-9]+$/)
  })

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
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-3"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-3", change_request: {id: "change-1"}})))
    vi.stubGlobal("fetch", fetch)

    await createChangeRequest("incident/1", {
      desired_outcome: "恢复服务",
      context: "发布后异常",
      idempotency_key: "create-1",
    })
    await submitChangeRequestInput("change/1", {content: "gateway-v41", idempotency_key: "input-1"})
    await retryChangeRequestPlanning("change/1")

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/incidents/incident%2F1/change-requests",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-1"})}),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      6,
      "/api/v1/change-requests/change%2F1/retry",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-3"})}),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/api/v1/change-requests/change%2F1/input",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-2"})}),
    )
  })

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

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/chat/sessions/chat%2F1/messages",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({"X-CSRF-Token": "csrf-chat"}),
        body: JSON.stringify({content: "解释 Deployment", idempotency_key: "message-1"}),
      }),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/api/v1/chat/sessions/chat%2F1/messages/message%2F1/retry",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-retry"})}),
    )
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

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/chat/sessions/chat%2F1/messages",
      expect.objectContaining({
        body: JSON.stringify({
          content: "检查错误率",
          idempotency_key: "message-2",
          scope: {cluster_id: "cluster-prod", deployment_target_id: "target-checkout"},
        }),
      }),
    )
  })

  it("submits an explicit Chat Handoff through the creator-scoped CSRF route", async () => {
    const handoff = {id: "handoff-1", incident_id: "incident-1", idempotent: false}
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-handoff"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-handoff", handoff})))
    vi.stubGlobal("fetch", fetch)

    expect(await createChatHandoff(
      "chat/1", ["message/1"],
      {type: "existing_incident", incident_id: "incident/1"},
      "handoff-1",
    )).toEqual(handoff)

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/chat/sessions/chat%2F1/handoffs",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({"X-CSRF-Token": "csrf-handoff"}),
        body: JSON.stringify({
          message_ids: ["message/1"], idempotency_key: "handoff-1",
          target: {type: "existing_incident", incident_id: "incident/1"},
        }),
      }),
    )
  })

  it("creates Secure Input through the shared CSRF client", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-secure"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        request_id: "req-secure", secure_input: {
          id: "opaque-1", key_name: "api.token", placeholder: "{{secure-input:opaque-1}}",
          sha256: "a".repeat(64), source: "generated", created_at: 1, expires_at: 2,
          available: true, idempotent: false,
        },
      })))
    vi.stubGlobal("fetch", fetch)

    const secure = await createSecureInput({
      key_name: "api.token", generated_bytes: 32, idempotency_key: "secure-1",
    })

    expect(secure.placeholder).toBe("{{secure-input:opaque-1}}")
    expect(fetch).toHaveBeenNthCalledWith(
      2, "/api/v1/secure-inputs",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-secure"})}),
    )
  })

  it("manages MCP Integrations through masked admin routes", async () => {
    const integration = {
      id: "mcp-1", name: "Prometheus", endpoint: "https://mcp.example.test",
      credential_configured: true, capabilities: [], allowed_scope: [], enabled: true,
      revision: "mcp-revision:1", verification: {}, health: {},
      capability_snapshot: null, verified_capability_snapshot: null,
      capability_changed: false, created_at: 1, updated_at: 1,
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "list", mcp_integrations: [integration]})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-create"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "create", mcp_integration: integration}), {status: 201}))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-update"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "update", mcp_integration: integration})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-verify"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "verify", mcp_integration: integration})))
    vi.stubGlobal("fetch", fetch)
    const body = {
      name: "Prometheus", endpoint: "https://mcp.example.test", credential: "transient-secret",
      capabilities: [{name: "query_metrics", version: "prometheus-query-v1", read_only: true}],
      allowed_scope: [{cluster_id: "cluster-prod", namespace: "payments"}], enabled: true,
      reason: "register metrics",
    }

    expect(await getMCPIntegrations()).toEqual([integration])
    await createMCPIntegration(body)
    await updateMCPIntegration("mcp/1", {...body, expected_revision: "mcp-revision:1"})
    await verifyMCPIntegration("mcp/1", "verify tools")

    expect(fetch).toHaveBeenNthCalledWith(1, "/api/v1/admin/mcp-integrations", expect.anything())
    expect(fetch).toHaveBeenNthCalledWith(3, "/api/v1/admin/mcp-integrations", expect.objectContaining({
      method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-create"}),
    }))
    expect(fetch).toHaveBeenNthCalledWith(5, "/api/v1/admin/mcp-integrations/mcp%2F1", expect.objectContaining({method: "PATCH"}))
    expect(fetch).toHaveBeenNthCalledWith(7, "/api/v1/admin/mcp-integrations/mcp%2F1/verify", expect.objectContaining({method: "POST"}))
  })

  it("reads actor and request-linked administration audit", async () => {
    const audit = [{
      id: 1, actor_id: "user:admin", target_type: "mcp_integration", target_id: "mcp-1",
      action: "mcp_integration_verify", reason: "verify", before: null, after: null,
      result: "success", request_id: "req-audit", created_at: 1,
    }]
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({request_id: "list-audit", audit})))
    vi.stubGlobal("fetch", fetch)

    expect(await getAdminAudit()).toEqual(audit)
    expect(fetch).toHaveBeenCalledWith("/api/v1/admin/audit", expect.anything())
  })

  it("reuses a supplied Notification credential request ID for reconciliation", async () => {
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

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/notification-destinations",
      expect.objectContaining({headers: expect.objectContaining({"X-Request-ID": "notification-unknown"})}),
    )
  })

  it("binds Phase Approval and Authority writes to generated contract routes", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-approval"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", phase_review: {status: "approved"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-execution"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-2", phase_execution: {id: "execution-1"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-authority"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-3", kubernetes_change_authority: {id: "authority-1"}})))
    vi.stubGlobal("fetch", fetch)

    await approveKubernetesPhase("change/1", {
      revision_id: "revision-1",
      dry_run_hashes: ["a".repeat(64)],
      target_confirmations: ["apps/v1:Deployment:payments/checkout-api"],
      rollback_policy: "rollback_completed",
      reason: "restore service",
      idempotency_key: "approval-1",
    })
    await startKubernetesPhaseExecution("change/1", {
      phase_id: "phase-1",
      reason: "execute approved change",
      idempotency_key: "execution-1",
      execution_timeout_seconds: 300,
    })
    await createKubernetesChangeAuthority({
      user_id: "user-1",
      environment: "prod",
      scope_type: "namespace",
      scope: {cluster_id: "cluster-prod", namespace: "payments"},
      reason: "on-call authority",
    })

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/change-requests/change%2F1/phase-approval/approve",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-approval"})}),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      "/api/v1/change-requests/change%2F1/phase-execution/start",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-execution"})}),
    )
    expect(fetch).toHaveBeenNthCalledWith(
      6,
      "/api/v1/admin/kubernetes-change-authorities",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-authority"})}),
    )
  })

  it("sends execution cancellation through the CSRF write client", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-cancel"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        request_id: "req-cancel", phase_execution: {id: "execution-1", status: "cancelled"},
      })))
    vi.stubGlobal("fetch", fetch)

    await cancelKubernetesPhaseExecution("change/1", {
      phase_id: "phase-1", reason: "window closed", idempotency_key: "cancel-1",
    })

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/change-requests/change%2F1/phase-execution/cancel",
      expect.objectContaining({
        method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-cancel"}),
      }),
    )
  })

  it("accepts reconciliation evidence through the CSRF write client", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-reconcile"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        request_id: "req-reconcile", reconciliation: {id: "reconciliation-1"},
      })))
    vi.stubGlobal("fetch", fetch)

    await acceptKubernetesReconciliation("change/1", {
      phase_id: "phase-1", evidence_sha256: "a".repeat(64),
      reason: "accept observed live state", idempotency_key: "reconcile-1",
    })

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/change-requests/change%2F1/phase-execution/reconciliation/accept",
      expect.objectContaining({
        method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-reconcile"}),
      }),
    )
  })
})
