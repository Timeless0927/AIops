import { afterEach, describe, expect, it, vi } from "vitest"

import {
  acceptKubernetesReconciliation,
  approveKubernetesPhase,
  cancelKubernetesPhaseExecution,
  createChangeRequest,
  createSecureInput,
  retryChangeRequestPlanning,
  startKubernetesPhaseExecution,
  submitChangeRequestInput,
} from "./change-client"

describe("Change client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("submits Change Request writes through shared CSRF transport", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-1"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", change_request: {id: "change-1"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-2"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-2", change_request: {id: "change-1"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-3"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-3", change_request: {id: "change-1"}})))
    vi.stubGlobal("fetch", fetch)

    await createChangeRequest("incident/1", {desired_outcome: "恢复服务", context: "发布后异常", idempotency_key: "create-1"})
    await submitChangeRequestInput("change/1", {content: "gateway-v41", idempotency_key: "input-1"})
    await retryChangeRequestPlanning("change/1")

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/incidents/incident%2F1/change-requests", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(4, "/api/v1/change-requests/change%2F1/input", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(6, "/api/v1/change-requests/change%2F1/retry", expect.objectContaining({method: "POST"}))
  })

  it("creates Secure Input through shared CSRF transport", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-secure"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-secure", secure_input: {
        id: "opaque-1", key_name: "api.token", placeholder: "{{secure-input:opaque-1}}",
        sha256: "a".repeat(64), source: "generated", created_at: 1, expires_at: 2,
        available: true, idempotent: false,
      }})))
    vi.stubGlobal("fetch", fetch)

    const secure = await createSecureInput({key_name: "api.token", generated_bytes: 32, idempotency_key: "secure-1"})

    expect(secure.placeholder).toBe("{{secure-input:opaque-1}}")
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/secure-inputs", expect.objectContaining({method: "POST"}))
  })

  it("uses the generated Phase Approval and execution routes", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-approval"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", phase_review: {status: "approved"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-execution"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-2", phase_execution: {id: "execution-1"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-cancel"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-3", phase_execution: {id: "execution-1", status: "cancelled"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-reconcile"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-4", reconciliation: {id: "reconciliation-1"}})))
    vi.stubGlobal("fetch", fetch)

    await approveKubernetesPhase("change/1", {
      revision_id: "revision-1", dry_run_hashes: ["a".repeat(64)],
      target_confirmations: ["apps/v1:Deployment:payments/checkout-api"],
      rollback_policy: "rollback_completed", reason: "restore service", idempotency_key: "approval-1",
    })
    await startKubernetesPhaseExecution("change/1", {
      phase_id: "phase-1", reason: "execute approved change", idempotency_key: "execution-1", execution_timeout_seconds: 300,
    })
    await cancelKubernetesPhaseExecution("change/1", {phase_id: "phase-1", reason: "window closed", idempotency_key: "cancel-1"})
    await acceptKubernetesReconciliation("change/1", {
      phase_id: "phase-1", evidence_sha256: "a".repeat(64), reason: "accept observed live state", idempotency_key: "reconcile-1",
    })

    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/change-requests/change%2F1/phase-approval/approve", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(4, "/api/v1/change-requests/change%2F1/phase-execution/start", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(6, "/api/v1/change-requests/change%2F1/phase-execution/cancel", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(8, "/api/v1/change-requests/change%2F1/phase-execution/reconciliation/accept", expect.objectContaining({method: "POST"}))
  })
})
