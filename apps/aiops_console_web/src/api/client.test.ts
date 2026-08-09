import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createNotificationDestination,
  createMCPIntegration,
  createSkill,
  createSkillVersion,
  createKubernetesChangeAuthority,
  getAdminAudit,
  getMCPIntegrations,
  getSkills,
  disableSkill,
  enableSkill,
  updateMCPIntegration,
  verifyMCPIntegration,
} from "./client"
describe("API client request IDs", () => {
  afterEach(() => vi.unstubAllGlobals())

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

  it("manages immutable Skill versions through governed admin routes", async () => {
    const skill = {
      id: "skill-1", name: "Payments triage", enabled: true,
      active_version: 2, latest_version: 2, versions: [],
      availability: {state: "ready", reason_code: null}, created_at: 1, updated_at: 2,
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "list", skills: [skill]})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-create"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "create", skill}), {status: 201}))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-version"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "version", skill}), {status: 201}))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-enable"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "enable", skill})))
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-disable"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "disable", skill})))
    vi.stubGlobal("fetch", fetch)
    const content = {
      instruction: "Check error-rate Observation before concluding.",
      workflow: ["Query metrics", "Cite accepted Evidence"],
      applicable_scope: [{cluster_id: "cluster-prod", namespace: "payments"}],
      required_mcp: [{
        integration_id: "mcp-metrics", integration_revision: "mcp-revision:metrics",
        name: "query_metrics", version: "prometheus-query-v1",
      }],
      reason: "capture reviewed triage practice",
    }

    expect(await getSkills()).toEqual([skill])
    await createSkill({name: "Payments triage", ...content})
    await createSkillVersion("skill/1", content)
    await enableSkill("skill/1", 2, 1, "switch reviewed version")
    await disableSkill("skill/1", 2, "disable practice")

    expect(fetch).toHaveBeenNthCalledWith(1, "/api/v1/admin/skills", expect.anything())
    expect(fetch).toHaveBeenNthCalledWith(3, "/api/v1/admin/skills", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(5, "/api/v1/admin/skills/skill%2F1/versions", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(7, "/api/v1/admin/skills/skill%2F1/enable", expect.objectContaining({method: "POST"}))
    expect(fetch).toHaveBeenNthCalledWith(9, "/api/v1/admin/skills/skill%2F1/disable", expect.objectContaining({method: "POST"}))
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

  it("binds Authority writes to the generated contract route", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({csrf_token: "csrf-authority"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "req-1", kubernetes_change_authority: {id: "authority-1"}})))
    vi.stubGlobal("fetch", fetch)

    await createKubernetesChangeAuthority({
      user_id: "user-1",
      environment: "prod",
      scope_type: "namespace",
      scope: {cluster_id: "cluster-prod", namespace: "payments"},
      reason: "on-call authority",
    })

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/kubernetes-change-authorities",
      expect.objectContaining({method: "POST", headers: expect.objectContaining({"X-CSRF-Token": "csrf-authority"})}),
    )
  })
})
