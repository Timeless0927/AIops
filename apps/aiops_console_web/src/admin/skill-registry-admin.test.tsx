import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { SkillRegistryAdminView } from "@/admin/skill-registry-admin"


describe("SkillRegistryAdminView", () => {
  it("renders immutable versions, exact dependencies, controls and request-linked audit", () => {
    const markup = renderToStaticMarkup(<SkillRegistryAdminView
      skills={[{
        id: "skill-payments", name: "Payments triage", enabled: true,
        active_version: 1, latest_version: 2,
        versions: [{
          version: 1,
          instruction: "Check error-rate Observation before concluding.",
          workflow: ["Query metrics", "Cite accepted Evidence"],
          applicable_scope: [{cluster_id: "cluster-prod", namespace: "payments"}],
          required_mcp: [{
            integration_id: "mcp-metrics", integration_revision: "mcp-revision:metrics",
            name: "query_metrics", version: "prometheus-query-v1",
          }],
          created_by: "user:admin", reason: "reviewed practice", created_at: 1_700_000_000,
          dependency: {state: "ready", reason_code: null},
        }, {
          version: 2,
          instruction: "Check saturation before concluding.",
          workflow: ["Query saturation", "Cite accepted Evidence"],
          applicable_scope: [{cluster_id: "cluster-prod", namespace: "payments"}],
          required_mcp: [{
            integration_id: "mcp-metrics", integration_revision: "mcp-revision:metrics",
            name: "query_metrics", version: "prometheus-query-v2",
          }],
          created_by: "user:admin", reason: "refine first check", created_at: 1_700_000_100,
          dependency: {state: "unavailable", reason_code: "mcp_capability_changed"},
        }],
        availability: {state: "ready", reason_code: null},
        created_at: 1_700_000_000, updated_at: 1_700_000_100,
      }]}
      audit={[{
        id: 9, actor_id: "user:admin", target_type: "skill", target_id: "skill-payments",
        action: "skill_enable", reason: "enable reviewed version", before: null, after: null,
        result: "success", request_id: "req-skill-enable", created_at: 1_700_000_200,
      }]}
      reason="reviewed change"
      pending={false}
      error={null}
      onCreate={vi.fn()}
      onCreateVersion={vi.fn()}
      onEnable={vi.fn()}
      onDisable={vi.fn()}
    />)

    for (const expected of [
      "Payments triage", "当前 v1", "最新 v2", "版本历史", "依赖可用", "依赖不可用",
      "mcp_capability_changed", "cluster-prod / payments", "mcp-metrics", "mcp-revision:metrics",
      "query_metrics", "prometheus-query-v2", "Check saturation before concluding.",
      "切换到 v2", "停用", "user:admin", "req-skill-enable", "skill_enable",
    ]) expect(markup).toContain(expected)
    expect(markup).not.toContain('type="file"')
    expect(markup).not.toContain("script")
  })
})
