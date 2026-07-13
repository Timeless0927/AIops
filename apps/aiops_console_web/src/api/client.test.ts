import { afterEach, describe, expect, it, vi } from "vitest"

import {
  ApiError,
  approveKubernetesPhase,
  createChangeRequest,
  createKubernetesChangeAuthority,
  getActor,
  retryChangeRequestPlanning,
  submitChangeRequestInput,
} from "./client"

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

  it("binds Phase Approval and Authority writes to generated contract routes", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-approval"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", phase_review: {status: "approved"}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-authority"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-2", kubernetes_change_authority: {id: "authority-1"}})))
    vi.stubGlobal("fetch", fetch)

    await approveKubernetesPhase("change/1", {
      revision_id: "revision-1",
      dry_run_hashes: ["a".repeat(64)],
      target_confirmations: ["apps/v1:Deployment:payments/checkout-api"],
      rollback_policy: "rollback_completed",
      reason: "restore service",
      idempotency_key: "approval-1",
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
      "/api/v1/admin/kubernetes-change-authorities",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-authority"})}),
    )
  })
})
