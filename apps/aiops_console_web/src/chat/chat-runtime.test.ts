import { describe, expect, it } from "vitest"

import type { ChatSession } from "@/chat/chat-client"
import { applyChatEvent, chatAttachmentAdapter, chatMessageRepository, chatThreadListAdapter, textFromAssistantMessage } from "@/chat/chat-runtime"

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
    {id: "user-1", role: "user", status: "completed", content: "检查错误率", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], attachments: [{id: "attachment-1", session_id: "chat-1", filename: "error.png", content_type: "image/png", size: 1024, sha256: "abc", status: "ready", parse_state: "ready", extraction_sha256: "", model_use_status: "included", rejection_code: null, message_id: "user-1", created_at: 1, updated_at: 1}], created_at: 1, updated_at: 1},
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
    expect(repository.messages[0]?.message.attachments).toMatchObject([{
      id: "attachment-1",
      type: "image",
      content: [{type: "image", image: "/api/v1/chat/sessions/chat-1/attachments/attachment-1/download"}],
    }])
  })

  it("merges replayable assistant deltas without duplicating an event", () => {
    const running = {...session, messages: session.messages.map((message) => message.id === "assistant-1" ? {...message, status: "sending" as const, content: ""} : message)}
    const event = {id: 3, session_id: "chat-1", type: "message.delta" as const, payload: {message_id: "assistant-1", delta: "错误率"}, created_at: 3}
    const first = applyChatEvent(running, event)!
    const replay = applyChatEvent(first, event)!

    expect(first.messages[1]).toMatchObject({status: "sending", content: "错误率", updated_at: 3})
    expect(first.event_cursor).toBe(3)
    expect(replay.messages[1]?.content).toBe("错误率")
  })

  it("keeps Gateway attachment ids through upload, send, and remove", async () => {
    const changed: string[] = []
    const removed: string[] = []
    const attachment = {id: "attachment-1", session_id: "chat-1", filename: "incident.log", content_type: "text/plain", size: 12, sha256: "abc", status: "pending" as const, parse_state: "pending" as const, extraction_sha256: "", model_use_status: "not_used" as const, rejection_code: null, message_id: null, created_at: 1, updated_at: 1}
    const adapter = chatAttachmentAdapter({
      reserve: async () => attachment,
      upload: async () => ({...attachment, status: "ready", parse_state: "ready"}),
      retry: async () => ({...attachment, status: "ready", parse_state: "ready"}),
      remove: async (id) => { removed.push(id) },
      onChange: (value) => { changed.push(value.status) },
    })
    const states = []
    const addition = adapter.add({file: new File(["log"], "incident.log", {type: "text/plain"})})
    if (!(Symbol.asyncIterator in addition)) throw new Error("expected attachment lifecycle")
    for await (const state of addition) states.push(state)

    expect(states).toMatchObject([
      {id: "attachment-1", status: {type: "running", reason: "uploading", progress: 0}},
      {id: "attachment-1", status: {type: "requires-action", reason: "composer-send"}},
    ])
    expect(changed).toEqual(["pending", "ready"])
    await expect(adapter.send(states[1]!)).resolves.toMatchObject({id: "attachment-1", status: {type: "complete"}, content: []})
    await adapter.retry("attachment-1")
    expect(changed).toEqual(["pending", "ready", "ready"])
    await adapter.remove(states[1]!)
    expect(removed).toEqual(["attachment-1"])
  })

  it("keeps a rejected Gateway attachment in an incomplete composer state", async () => {
    const attachment = {id: "attachment-rejected", session_id: "chat-1", filename: "secret.txt", content_type: "text/plain", size: 12, sha256: "", status: "pending" as const, parse_state: "pending" as const, extraction_sha256: "", model_use_status: "not_used" as const, rejection_code: null, message_id: null, created_at: 1, updated_at: 1}
    const adapter = chatAttachmentAdapter({reserve: async () => attachment, upload: async () => ({...attachment, status: "rejected", parse_state: "rejected", rejection_code: "sensitive_content"}), retry: async () => attachment, remove: async () => undefined, onChange: () => undefined})
    const addition = adapter.add({file: new File(["secret"], "secret.txt", {type: "text/plain"})})
    if (!(Symbol.asyncIterator in addition)) throw new Error("expected attachment lifecycle")
    const states = []
    for await (const state of addition) states.push(state)
    expect(states.at(-1)).toMatchObject({id: "attachment-rejected", status: {type: "incomplete", reason: "error", message: "sensitive_content"}})
  })

  it("extracts only text parts for Gateway message mutations", () => {
    expect(textFromAssistantMessage([{type: "text", text: "检查"}, {type: "image"}, {type: "text", text: "日志"}])).toBe("检查日志")
  })

  it("routes thread list operations through the Gateway callbacks", async () => {
    const calls: string[] = []
    const adapter = chatThreadListAdapter({
      threadId: "chat-1",
      sessions: [{id: "chat-1", title: "排查 checkout", created_at: 1, updated_at: 2, expires_at: null, message_count: 2, selected_scope: null, pinned: false, archived: false, title_manual: false}],
      archived: false,
      onCreate: () => { calls.push("create") },
      onSelect: (id) => { calls.push(`select:${id}`) },
      onRename: (id, title) => { calls.push(`rename:${id}:${title}`) },
      onPin: (id, pinned) => { calls.push(`pin:${id}:${pinned}`) },
      onArchive: (id, archived) => { calls.push(`archive:${id}:${archived}`) },
      onDelete: (id) => { calls.push(`delete:${id}`) },
    })

    expect(adapter.threads).toMatchObject([{id: "chat-1", status: "regular", custom: {pinned: false}}])
    await adapter.onSwitchToNewThread?.()
    await adapter.onSwitchToThread?.("chat-1")
    await adapter.onRename?.("chat-1", "新的标题")
    await adapter.onUpdateCustom?.("chat-1", {pinned: true})
    await adapter.onArchive?.("chat-1")
    await adapter.onUnarchive?.("chat-1")
    await adapter.onDelete?.("chat-1")

    expect(calls).toEqual([
      "create", "select:chat-1", "rename:chat-1:新的标题", "pin:chat-1:true",
      "archive:chat-1:true", "archive:chat-1:false", "delete:chat-1",
    ])
    const archived = chatThreadListAdapter({
      threadId: "chat-1",
      sessions: [{id: "chat-1", title: "排查 checkout", created_at: 1, updated_at: 2, expires_at: null, message_count: 2, selected_scope: null, pinned: false, archived: true, title_manual: false}],
      archived: true,
      onCreate: () => undefined,
      onSelect: () => undefined,
      onRename: () => undefined,
      onPin: () => undefined,
      onArchive: () => undefined,
      onDelete: () => undefined,
    })
    expect(archived.threads).toEqual([])
    expect(archived.archivedThreads).toMatchObject([{id: "chat-1", status: "archived"}])
  })

  it("reuses an empty thread before creating another one", async () => {
    const calls: string[] = []
    const adapter = chatThreadListAdapter({
      threadId: "empty-chat",
      sessions: [
        {id: "empty-chat", title: "新对话", created_at: 1, updated_at: 1, expires_at: null, message_count: 0, selected_scope: null, pinned: false, archived: false, title_manual: false},
      ],
      archived: false,
      onCreate: () => { calls.push("create") },
      onSelect: (id) => { calls.push(`select:${id}`) },
      onRename: () => undefined,
      onPin: () => undefined,
      onArchive: () => undefined,
      onDelete: () => undefined,
    })

    await adapter.onSwitchToNewThread?.()
    await adapter.onSwitchToNewThread?.()

    expect(calls).toEqual([])
  })

  it("maps a failed assistant message to an incomplete runtime status", () => {
    const failed = chatMessageRepository({...session, messages: [{...session.messages[1]!, status: "failed"}]})
    expect(failed.messages[0]?.message.status).toEqual({type: "incomplete", reason: "error"})
  })
})
