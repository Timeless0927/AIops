import { describe, expect, it } from "vitest"

import type { ChatSession } from "@/api/client"
import { chatMessageRepository, textFromAssistantMessage } from "@/chat/chat-runtime"

const session: ChatSession = {
  id: "chat-1",
  title: "排查 checkout",
  created_at: 1,
  updated_at: 2,
  expires_at: null,
  message_count: 2,
  selected_scope: null,
  pinned: false,
  archived: false,
  title_manual: false,
  event_cursor: 2,
  current_branch_head_id: "assistant-1",
  messages: [
    {id: "user-1", role: "user", status: "completed", content: "检查错误率", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], attachments: [], created_at: 1, updated_at: 1},
    {id: "assistant-1", role: "assistant", status: "completed", content: "错误率正常。", parent_id: "user-1", reply_to_id: "user-1", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "environment", scope: null, tool_activity: [{tool: "query_metrics", status: "succeeded", summary: "error_rate=0.01", authorized_scope: {deployment_target_id: "checkout"}, skill_versions: []}], evidence_references: ["evidence:1"], uncertainty: {status: "accepted", reasons: []}, next_step: "继续观察", completion: {status: "accepted", stopping_reason: "validated"}, skill_versions: [{id: "skill-1", name: "排障", version: 1}], attachments: [], created_at: 2, updated_at: 2},
  ],
}

describe("chatMessageRepository", () => {
  it("keeps Gateway identities, branches, status, and governed extensions", () => {
    const repository = chatMessageRepository(session)

    expect(repository.headId).toBe("assistant-1")
    expect(repository.messages).toHaveLength(2)
    expect(repository.messages[1]).toMatchObject({parentId: "user-1", message: {id: "assistant-1", createdAt: new Date(2000), status: {type: "complete", reason: "stop"}}})
    expect(repository.messages[1]?.message.metadata?.custom?.gateway).toMatchObject({
      mode: "environment",
      toolActivity: [{tool: "query_metrics"}],
      evidenceReferences: ["evidence:1"],
      uncertainty: {status: "accepted"},
      nextStep: "继续观察",
      completion: {status: "accepted"},
      skillVersions: [{id: "skill-1"}],
    })
  })

  it("extracts only text parts for Gateway message mutations", () => {
    expect(textFromAssistantMessage([{type: "text", text: "检查"}, {type: "image"}, {type: "text", text: "日志"}])).toBe("检查日志")
  })
})
