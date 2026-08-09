import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import type { InvestigationEvent, Workbench } from "@/incidents/incident-client"
import { DecisionTrace } from "@/prototype/decision-trace"

const event: InvestigationEvent = {
  id: 7,
  investigation_id: "investigation-1",
  type: "diagnosis.output",
  actor_id: null,
  created_at: 1_000,
  payload: {
    decision_trace: {
      goal: "定位 checkout-api 错误率升高原因",
      skill_versions: [{id: "skill-payments", name: "Payments triage", version: 2}],
      tool_activity: [
        {
          tool: "query_logs",
          purpose: "检查 Loki 日志",
          authorized_scope: {cluster_id: "prod-a", namespace: "payments"},
          time_range: {time_range: {type: "relative", value: "30m"}},
          status: "partial",
          duration_ms: 125,
          summary: "发现 upstream timeout",
          evidence_references: ["evidence:logs:1"],
          candidate_impacts: [{cause: "upstream timeout", relation: "supports"}],
          truncation: {truncated: true, limit_bytes: 4096, reason: "已按 4096 字节安全上限截断"},
          redaction: {applied: true, note: "敏感字段已移除"},
          continuation_reason: "继续检查 Prometheus",
        },
        {
          tool: "query_metrics",
          purpose: "检查 Prometheus 指标",
          authorized_scope: {cluster_id: "prod-a", namespace: "payments"},
          time_range: {},
          status: "failed",
          duration_ms: 80,
          summary: "Prometheus unavailable",
          missing_reason: "backend timeout",
          status_reason: "backend timeout",
          evidence_references: [],
          candidate_impacts: [],
          truncation: {truncated: false, limit_bytes: 4096},
          redaction: {applied: false, note: "未报告脱敏"},
          stopping_reason: "required_source_missing",
          skill_versions: [{id: "skill-payments", name: "Payments triage", version: 2}],
        },
        {
          tool: "query_logs",
          purpose: "补充检查历史日志",
          authorized_scope: {cluster_id: "prod-a", namespace: "payments"},
          time_range: {},
          status: "skipped",
          duration_ms: null,
          summary: "Evidence budget 已耗尽",
          status_reason: "Evidence budget 已耗尽",
          evidence_references: [],
          candidate_impacts: [],
          truncation: {truncated: false, limit_bytes: 4096},
          redaction: {applied: false, note: "未报告脱敏"},
          stopping_reason: "evidence_budget_exhausted",
        },
      ],
      candidates: [
        {
          cause: "upstream timeout",
          confidence: 0.72,
          evidence_relations: [
            {evidence_ref: "evidence:logs:1", relation: "supports"},
            {evidence_ref: "evidence:metrics:1", relation: "refutes"},
          ],
          unknowns: ["upstream saturation 尚未确认"],
          next_checks: [],
        },
      ],
      completion: {
        status: "accepted",
        issues: [],
        repair_attempts: 0,
        stopping_reason: "required_source_missing",
        remaining_evidence_steps: 22,
      },
    },
  },
}

const judgment: NonNullable<Workbench["judgment"]> = {
  summary: "日志支持 upstream timeout，指标证据缺失",
  evidence_gate_status: "incomplete",
  next_evidence_guidance: ["恢复 Prometheus 后重新查询"],
  valid: true,
}

describe("DecisionTrace", () => {
  it("renders readable tool, candidate, gate, and stopping facts without raw JSON", () => {
    const markup = renderToStaticMarkup(<DecisionTrace events={[event]} judgment={judgment} />)

    for (const expected of [
      "决策轨迹（Decision Trace）",
      "Payments triage v2",
      "定位 checkout-api 错误率升高原因",
      "检查 Loki 日志",
      "125 ms",
      "evidence:logs:1",
      "支持 upstream timeout",
      "已截断",
      "已按 4096 字节安全上限截断",
      "已脱敏",
      "敏感字段已移除",
      "状态原因",
      "Evidence budget 已耗尽",
      "backend timeout",
      "required_source_missing",
      "upstream timeout",
      "72%（仅供参考）",
      "反驳 evidence:metrics:1",
      "Evidence Gate：不完整",
    ]) expect(markup).toContain(expected)
    expect(markup).toContain("<details")
    expect(markup).not.toContain("{&quot;")
  })

  it("renders an explicit empty state before diagnosis output", () => {
    expect(renderToStaticMarkup(<DecisionTrace events={[]} judgment={null} />)).toContain("尚无决策轨迹")
  })
})
