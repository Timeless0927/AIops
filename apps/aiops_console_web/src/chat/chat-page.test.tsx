import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import { ApiError } from "@/api/transport"
import { type ChatAttachment, type ChatSession } from "@/chat/chat-client"
import { chatErrorMessage, ChatView, HandoffSuccessContent } from "@/chat/chat-page"
import { Dialog } from "@/components/ui/dialog"

const sessions = [{
  id: "chat-1",
  title: "Deployment 如何管理 Pod？",
  created_at: 1,
  updated_at: 2,
  expires_at: 3,
  message_count: 4,
  selected_scope: null,
  pinned: false,
  archived: false,
  title_manual: false,
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
  current_branch_head_id: "m2",
  selected_scope: scope,
  messages: [
    {id: "m1", role: "user", status: "completed", content: "解释 Deployment", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "environment", scope, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 1, updated_at: 1},
    {id: "m2", role: "assistant", status: "completed", content: "Deployment 管理 ReplicaSet。", parent_id: "m1", reply_to_id: "m1", branch_index: 1, branch_count: 3, is_current_branch: true, error_code: null, mode: "environment", scope, tool_activity: [{tool: "query_metrics", status: "succeeded", summary: "error_rate=0.42", authorized_scope: {deployment_target_id: "target-checkout"}, skill_versions: skillVersions}], evidence_references: ["evidence:metrics:1"], uncertainty: {status: "accepted", reasons: []}, next_step: "继续观察错误率。", completion: {status: "accepted", stopping_reason: "validated"}, skill_versions: skillVersions, created_at: 1, updated_at: 1},
    {id: "m3", role: "assistant", status: "sending", content: "", parent_id: "m1", reply_to_id: "m1", branch_index: 2, branch_count: 3, is_current_branch: false, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 2, updated_at: 2},
    {id: "m4", role: "assistant", status: "failed", content: "暂时无法回答，请重试。", parent_id: "m1", reply_to_id: "m1", branch_index: 3, branch_count: 3, is_current_branch: false, error_code: "model_unavailable", mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 3, updated_at: 3},
  ],
}

const attachments: ChatAttachment[] = [
  {id: "attachment-1", session_id: "chat-1", filename: "incident.log", content_type: "text/plain", size: 3, sha256: "a".repeat(64), status: "ready", parse_state: "ready", extraction_sha256: "a".repeat(64), model_use_status: "not_used", rejection_code: null, message_id: null, created_at: 1, updated_at: 2},
  {id: "attachment-2", session_id: "chat-1", filename: "secret.txt", content_type: "text/plain", size: 4, sha256: "", status: "rejected", parse_state: "rejected", extraction_sha256: "", model_use_status: "not_used", rejection_code: "sensitive_content", message_id: null, created_at: 1, updated_at: 2},
  {id: "attachment-3", session_id: "chat-1", filename: "retry.log", content_type: "text/plain", size: 4, sha256: "", status: "failed", parse_state: "failed", extraction_sha256: "", model_use_status: "not_used", rejection_code: "scanner_unavailable", message_id: null, created_at: 1, updated_at: 2},
]

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
  it("shows governed Handoff failures in Chinese", () => {
    expect(chatErrorMessage(new ApiError(409, "investigation_terminal", "terminal"))).toBe("目标 Investigation 已结束，不能接收 Human Input。")
    expect(chatErrorMessage(new ApiError(404, "handoff_target_not_found", "missing"))).toBe("目标 Incident 不存在或无权访问。")
  })

  it("shows attachment lifecycle controls without sending unready files", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={sessions}
        session={session}
        attachments={attachments}
        pendingContent={null}
        connection="connected"
        resources={resources}
        incidents={incidents}
        selectedTargetId="knowledge"
        busy={false}
        error={null}
        handoff={null}
        actionBusy={false}
        query=""
        filter="all"
        onCreate={() => undefined}
        onSelect={() => undefined}
        onQueryChange={() => undefined}
        onFilterChange={() => undefined}
        onRename={() => undefined}
        onPin={() => undefined}
        onArchive={() => undefined}
        onDelete={() => undefined}
        onSend={() => undefined}
        onRemoveAttachment={() => undefined}
        onRetryAttachment={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onEdit={() => undefined}
        onReload={() => undefined}
        onSwitchBranch={() => undefined}
        onHandoff={() => undefined}
      />,
    )
    expect(markup).toContain("选择附件")
    expect(markup).toContain("incident.log")
    expect(markup).toContain("已拒绝")
    expect(markup).toContain("疑似凭据或 Secure Input")
    expect(markup).toContain("解析状态：解析完成")
    expect(markup).toContain("重试")
    expect(markup).toContain("移除")
    expect(markup).not.toContain("<details open")
  })

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
        actionBusy={false}
        query=""
        filter="all"
        onCreate={() => undefined}
        onSelect={() => undefined}
        onQueryChange={() => undefined}
        onFilterChange={() => undefined}
        onRename={() => undefined}
        onPin={() => undefined}
        onArchive={() => undefined}
        onDelete={() => undefined}
        onSend={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onEdit={() => undefined}
        onReload={() => undefined}
        onSwitchBranch={() => undefined}
        onHandoff={() => undefined}
      />,
    )

    for (const expected of [
      "AI 对话", "Deployment 如何管理 Pod？", "解释 Deployment", "Deployment 管理 ReplicaSet。",
      "正在提交的问题", "重新生成", "长期保留",
      "连接已断开，正在恢复实时更新",
      "cluster-prod / shop / Deployment / checkout-api", "query_metrics", "成功",
      "error_rate=0.42", "evidence:metrics:1", "已接受", "继续观察错误率。",
      "选择此消息", "搜索对话",
    ]) expect(markup).toContain(expected)
    expect(markup).toContain("停止生成")
    expect(markup).not.toContain("正在发送")
    expect(markup).toContain('aria-label="AI 对话会话"')
    expect(markup).toContain('aria-label="输入消息"')
    expect(markup).not.toContain("转交目标类型")
    expect(markup).not.toContain("核对转交内容")
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
        actionBusy={false}
        query=""
        filter="all"
        onCreate={() => undefined}
        onSelect={() => undefined}
        onQueryChange={() => undefined}
        onFilterChange={() => undefined}
        onRename={() => undefined}
        onPin={() => undefined}
        onArchive={() => undefined}
        onDelete={() => undefined}
        onSend={() => undefined}
        onScopeChange={() => undefined}
        onRetry={() => undefined}
        onEdit={() => undefined}
        onReload={() => undefined}
        onSwitchBranch={() => undefined}
        onHandoff={() => undefined}
      />,
    )
    expect(markup).toContain("新建对话")
    expect(markup).toContain("尚无 AI 对话会话")
  })

  it("shows idempotent Handoff completion without treating copied content as Evidence", () => {
    const markup = renderToStaticMarkup(
      <Dialog open>
        <HandoffSuccessContent
          handoff={{
            id: "handoff-1", chat_session_id: "chat-1", target_type: "existing_incident",
            incident_id: "incident-1", investigation_id: "investigation-1",
            selected_message_ids: ["m1"], created_at: 4, idempotent: true,
          }}
        />
      </Dialog>,
    )

    expect(markup).toContain("重复请求已安全返回")
    expect(markup).toContain("incident-1")
    expect(markup).toContain("不会自动成为 Evidence")
    expect(markup).toContain("进入事件调查")
    expect(markup).toContain("留在 AI 对话")
  })
})
