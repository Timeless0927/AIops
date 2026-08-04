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
}]

const session: ChatSession = {
  ...sessions[0],
  event_cursor: 7,
  messages: [
    {id: "m1", role: "user", status: "completed", content: "解释 Deployment", reply_to_id: null, error_code: null, created_at: 1, updated_at: 1},
    {id: "m2", role: "assistant", status: "completed", content: "Deployment 管理 ReplicaSet。", reply_to_id: "m1", error_code: null, created_at: 1, updated_at: 1},
    {id: "m3", role: "assistant", status: "sending", content: "", reply_to_id: "m1", error_code: null, created_at: 2, updated_at: 2},
    {id: "m4", role: "assistant", status: "failed", content: "暂时无法回答，请重试。", reply_to_id: "m1", error_code: "model_unavailable", created_at: 3, updated_at: 3},
  ],
}

describe("ChatView", () => {
  it("renders history, transient send state, failure retry, and retention guidance", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={sessions}
        session={session}
        pendingContent="正在提交的问题"
        connection="reconnecting"
        busy={true}
        error={null}
        onCreate={() => undefined}
        onSelect={() => undefined}
        onSend={() => undefined}
        onRetry={() => undefined}
      />,
    )

    for (const expected of [
      "Chat", "Deployment 如何管理 Pod？", "解释 Deployment", "Deployment 管理 ReplicaSet。",
      "正在提交的问题", "正在回答", "暂时无法回答，请重试。", "重试", "保留 30 天",
      "连接已断开，正在恢复实时更新",
    ]) expect(markup).toContain(expected)
    expect(markup).toContain('aria-label="Chat 会话"')
    expect(markup).toContain('aria-label="输入消息"')
    expect(markup).not.toContain("Evidence")
    expect(markup).not.toContain("Approval")
  })

  it("offers session creation when history is empty", () => {
    const markup = renderToStaticMarkup(
      <ChatView
        sessions={[]}
        session={null}
        pendingContent={null}
        connection="connected"
        busy={false}
        error={null}
        onCreate={() => undefined}
        onSelect={() => undefined}
        onSend={() => undefined}
        onRetry={() => undefined}
      />,
    )
    expect(markup).toContain("新建对话")
    expect(markup).toContain("尚无 Chat Session")
  })
})
