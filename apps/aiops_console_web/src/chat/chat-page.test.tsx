import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import type { ChatSession } from "@/api/client"
import { ChatView } from "@/chat/chat-page"

const sessions = [{
  id: "chat-1",
  title: "Deployment 如何管理 Pod？",
  created_at: 1,
  updated_at: 2,
  expires_at: 3,
  message_count: 4,
  selected_scope: null,
}]

const scope = {
  selection: {cluster_id: "cluster-prod", deployment_target_id: "target-checkout"},
  resources: [{
    deployment_target_id: "target-checkout", cluster_id: "cluster-prod", namespace: "shop",
    service_id: "service-checkout", service_name: "checkout", workload_kind: "Deployment",
    workload_name: "checkout-api",
  }],
  time_range: {type: "relative" as const, value: "30m" as const},
  revision: "a".repeat(64),
}
const skillVersions = [{id: "skill-payments", name: "Payments triage", version: 2}]

const session: ChatSession = {
  ...sessions[0],
  event_cursor: 7,
  selected_scope: scope,
  messages: [
    {id: "m1", role: "user", status: "completed", content: "解释 Deployment", reply_to_id: null, error_code: null, mode: "environment", scope, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 1, updated_at: 1},
    {id: "m2", role: "assistant", status: "completed", content: "Deployment 管理 ReplicaSet。", reply_to_id: "m1", error_code: null, mode: "environment", scope, tool_activity: [{tool: "query_metrics", status: "succeeded", summary: "error_rate=0.42", authorized_scope: {deployment_target_id: "target-checkout"}, skill_versions: skillVersions}], evidence_references: ["evidence:metrics:1"], uncertainty: {status: "accepted", reasons: []}, next_step: "继续观察错误率。", completion: {status: "accepted", stopping_reason: "validated"}, skill_versions: skillVersions, created_at: 1, updated_at: 1},
    {id: "m3", role: "assistant", status: "sending", content: "", reply_to_id: "m1", error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 2, updated_at: 2},
    {id: "m4", role: "assistant", status: "failed", content: "暂时无法回答，请重试。", reply_to_id: "m1", error_code: "model_unavailable", mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 3, updated_at: 3},
  ],
}

const resources = [{
  id: "target-checkout", cluster_id: "cluster-prod", namespace: "shop", kind: "Deployment",
  name: "checkout-api", service_id: "service-checkout", team_id: "team-a",
  binding_state: "bound" as const, availability: "available" as const,
}]

const incidents = [{
  id: "incident-1", title: "checkout 错误率升高", severity: "critical" as const,
  status: "active" as const, lifecycle_state: "firing" as const, binding_status: "bound" as const,
  origin: "alert" as const, cluster_id: "cluster-prod", cluster_name: "prod", environment: "prod" as const,
  namespace: "shop", alertname: "HighErrorRate", workload_name: "checkout-api",
  service_name: "checkout", team_name: "Payments", signal_count: 1,
  diagnosis_outcome: null, evidence_gate_status: null, evidence_revision: 0,
  resolved_at: null, reopened_at: null, created_at: 1, updated_at: 2,
}]

describe("ChatView", () => {
  it("renders history, transient send state, failure retry, and retention guidance", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={sessions}
        session={session}
        pendingContent="正在提交的问题"
        connection="reconnecting"
        resources={resources}
        incidents={incidents}
        selectedTargetId="target-checkout"
        busy={true}
        error={null}
        handoff={null}
        onCreate={() => undefined}
        onSelect={() => undefined}
        onSend={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onHandoff={() => undefined}
      />,
    )

    for (const expected of [
      "Chat", "Deployment 如何管理 Pod？", "解释 Deployment", "Deployment 管理 ReplicaSet。",
      "正在提交的问题", "正在回答", "暂时无法回答，请重试。", "重试", "保留 30 天",
      "连接已断开，正在恢复实时更新",
      "cluster-prod / shop / Deployment / checkout-api", "query_metrics", "succeeded",
      "error_rate=0.42", "evidence:metrics:1", "accepted", "继续观察错误率。",
      "转交到 Investigation", "选择此消息", "已有 Incident", "User-created Incident",
      "核对转交内容", "Human Input", "checkout 错误率升高",
    ]) expect(markup).toContain(expected)
    expect(markup).toContain('aria-label="Chat 会话"')
    expect(markup).toContain('aria-label="输入消息"')
    expect(markup).toContain("不会成为 Evidence、Approval 或执行授权")
  })

  it("offers session creation when history is empty", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={[]}
        session={null}
        pendingContent={null}
        connection="connected"
        resources={[]}
        incidents={[]}
        selectedTargetId="knowledge"
        busy={false}
        error={null}
        handoff={null}
        onCreate={() => undefined}
        onSelect={() => undefined}
        onSend={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onHandoff={() => undefined}
      />,
    )
    expect(markup).toContain("新建对话")
    expect(markup).toContain("尚无 Chat Session")
  })

  it("shows idempotent Handoff completion without treating copied content as Evidence", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={sessions}
        session={session}
        pendingContent={null}
        connection="connected"
        resources={resources}
        incidents={incidents}
        selectedTargetId="target-checkout"
        busy={false}
        error={null}
        handoff={{
          id: "handoff-1", chat_session_id: "chat-1", target_type: "existing_incident",
          incident_id: "incident-1", investigation_id: "investigation-1",
          selected_message_ids: ["m1"], created_at: 4, idempotent: true,
        }}
        onCreate={() => undefined}
        onSelect={() => undefined}
        onSend={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onHandoff={() => undefined}
      />,
    )

    expect(markup).toContain("重复请求已安全返回")
    expect(markup).toContain("incident-1")
    expect(markup).toContain("不会成为 Evidence")
  })
})
