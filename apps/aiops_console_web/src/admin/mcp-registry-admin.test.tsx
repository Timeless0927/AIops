import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { MCPRegistryAdminView } from "@/admin/mcp-registry-admin"


describe("MCPRegistryAdminView", () => {
  it("renders masked health, drift, scope and bounded administration controls", () => {
    const markup = renderToStaticMarkup(<MCPRegistryAdminView
      integrations={[{
        id: "mcp-metrics", name: "Prometheus production", endpoint: "https://mcp.example.test",
        credential_configured: true,
        capabilities: [{name: "query_metrics", version: "prometheus-query-v1", read_only: true}],
        allowed_scope: [{cluster_id: "cluster-prod", namespace: "payments"}],
        enabled: true, revision: "mcp-revision:2",
        verification: {state: "failed", reason_code: "capability_snapshot_changed", verified_revision: null},
        health: {status: "ok", checked_at: 1_700_000_000, error: null},
        capability_snapshot: [{
          name: "query_metrics", version: "prometheus-query-v2", read_only: true,
          mutation: false, path: "/query_metrics",
        }],
        verified_capability_snapshot: [{
          name: "query_metrics", version: "prometheus-query-v1", read_only: true,
          mutation: false, path: "/query_metrics",
        }],
        capability_changed: true, created_at: 1_699_999_000, updated_at: 1_700_000_000,
      }]}
      audit={[{
        id: 7, actor_id: "user:admin", target_type: "mcp_integration", target_id: "mcp-metrics",
        action: "mcp_integration_use_denied", reason: "capability_snapshot_changed",
        before: null, after: null, result: "rejected", request_id: "req-audit-7", created_at: 1_700_000_100,
      }]}
      reason="review capability drift"
      pending={false}
      error={null}
      onCreate={vi.fn()}
      onUpdate={vi.fn()}
      onVerify={vi.fn()}
    />)

    expect(markup).toContain("Prometheus production")
    expect(markup).toContain("版本变化")
    expect(markup).toContain("prometheus-query-v1")
    expect(markup).toContain("prometheus-query-v2")
    expect(markup).toContain("cluster-prod / payments")
    expect(markup).toContain("凭据已配置")
    expect(markup).toContain('type="password"')
    expect(markup).toContain("验证")
    expect(markup).toContain("停用")
    expect(markup).toContain("user:admin")
    expect(markup).toContain("req-audit-7")
    expect(markup).toContain("mcp_integration_use_denied")
    expect(markup).toContain('for="mcp-edit-mcp-metrics-name"')
    expect(markup).not.toContain("mcp-runtime-secret")
  })
})
