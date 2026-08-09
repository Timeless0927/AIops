import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import type { InvestigationEvent } from "@/incidents/incident-client"
import { InvestigationEventAccordion } from "@/prototype/workbench-page"

describe("InvestigationEventAccordion", () => {
  it("keeps Handoff Human Input details collapsed while exposing retained material metadata", () => {
    const event: InvestigationEvent = {
      id: 7,
      investigation_id: "investigation-1",
      type: "human_input.assertion",
      actor_id: "user-1",
      payload: {
        content: "发布后错误率升高",
        source: "chat_handoff",
        content_sha256: "a".repeat(64),
        attachments: [{
          source_attachment_id: "attachment-1",
          retained_reference_id: "handoff:handoff-1:attachment:attachment-1",
          filename: "incident.log",
          content_type: "text/plain",
          size: 42,
          sha256: "b".repeat(64),
        }],
      },
      created_at: 1,
    }

    const markup = renderToStaticMarkup(<InvestigationEventAccordion events={[event]} canManage />)

    for (const expected of [
      "调查事件详情", "人工输入", "发布后错误率升高", "AI 对话 Handoff",
      "内容 SHA-256", "保留附件", "incident.log", "修正此输入", "撤回此输入",
    ]) expect(markup).toContain(expected)
    expect(markup).toContain('aria-expanded="false"')
    expect(markup).not.toContain('aria-expanded="true"')
  })
})
