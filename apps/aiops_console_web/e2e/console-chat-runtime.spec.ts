import { expect, test, type Page, type Route } from "@playwright/test"

const imagePngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

const baseSession = {
  id: "chat-runtime",
  title: "运行时适配",
  created_at: 1,
  updated_at: 2,
  expires_at: null,
  message_count: 2,
  selected_scope: null,
  pinned: false,
  archived: false,
  title_manual: false,
  current_branch_head_id: "assistant-failed",
  event_cursor: 2,
  messages: [
    {id: "user-1", role: "user", status: "completed", content: "检查运行时", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 1, updated_at: 1},
    {id: "assistant-failed", role: "assistant", status: "failed", content: "暂时无法回答，请重试。", parent_id: "user-1", reply_to_id: "user-1", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: "model_unavailable", mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 2, updated_at: 2},
  ],
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockRuntimeGateway(page: Page) {
  let current = structuredClone(baseSession)
  let sessionReads = 0
  let streamOpened = false
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") return json(route, {request_id: "actor", actor: {id: "user-runtime", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: [], is_platform_administrator: false}})
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") return json(route, {request_id: "list", chat_sessions: [current]})
    if (url.pathname === "/api/v1/chat/sessions/chat-runtime/attachments") return json(route, {request_id: "attachments", attachments: []})
    if (url.pathname === "/api/v1/chat/sessions/chat-runtime/events/stream") {
      if (!streamOpened) {
        streamOpened = true
        await new Promise((resolve) => setTimeout(resolve, 100))
        current = {...current, title: "SSE 刷新后的会话", event_cursor: 3}
        return route.fulfill({status: 200, headers: {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache"}, body: "id: 1\nevent: chat\ndata: {}\n\n"})
      }
      return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-runtime" && request.method() === "GET") {
      sessionReads += 1
      const response = structuredClone(current)
      if (sessionReads === 1) current = {...current, title: "SSE 刷新后的会话", event_cursor: 3}
      return json(route, {request_id: "get", chat_session: response})
    }
    if (url.pathname.endsWith("/assistant-failed/retry") && request.method() === "POST") {
      current = {...current, messages: current.messages.map((message) => message.id === "assistant-failed" ? {...message, status: "completed", content: "重试成功", error_code: null} : message)}
      return json(route, {request_id: "retry", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-runtime/messages" && request.method() === "POST") {
      const body = request.postDataJSON() as {content: string}
      current = {
        ...current,
        current_branch_head_id: "assistant-new",
        message_count: 4,
        messages: [
          ...current.messages.map((message) => ({...message, is_current_branch: true})),
          {id: "user-new", role: "user", status: "completed", content: body.content, parent_id: "assistant-failed", reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 3, updated_at: 3},
          {id: "assistant-new", role: "assistant", status: "completed", content: "发送成功", parent_id: "user-new", reply_to_id: "user-new", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], created_at: 4, updated_at: 4},
        ],
      }
      return json(route, {request_id: "send", chat_session: current})
    }
    await route.continue()
  })
  return () => sessionReads
}

async function mockAttachmentGateway(page: Page) {
  const image = Buffer.from(imagePngBase64, "base64")
  let attachments: Array<Record<string, unknown>> = []
  let attachmentSequence = 0
  let sentAttachmentIds: string[] = []
  let current = {
    ...structuredClone(baseSession),
    id: "chat-attachments",
    title: "附件与渐进披露",
    current_branch_head_id: "assistant-details",
    messages: [
      {...baseSession.messages[0], id: "user-details", content: "检查附件", attachments: []},
      {...baseSession.messages[1], id: "assistant-details", parent_id: "user-details", reply_to_id: "user-details", status: "completed", content: "核心答案保持可见。", error_code: null, tool_activity: [{tool: "query_metrics", status: "succeeded", summary: "error_rate=0.01", authorized_scope: {deployment_target_id: "checkout"}, skill_versions: []}], evidence_references: ["evidence:metrics:1"], uncertainty: {status: "accepted", reasons: []}, next_step: "继续观察。", completion: {status: "completed", stopping_reason: "validated"}, skill_versions: [{id: "skill-1", name: "排障", version: 1}], attachments: []},
    ],
  }
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") return json(route, {request_id: "actor", actor: {id: "user-attachments", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: [], is_platform_administrator: false}})
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") return json(route, {request_id: "list", chat_sessions: [current]})
    if (url.pathname === "/api/v1/chat/sessions/chat-attachments" && request.method() === "GET") return json(route, {request_id: "get", chat_session: current})
    if (url.pathname === "/api/v1/chat/sessions/chat-attachments/events/stream") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": reconnect\n\n"})
    if (url.pathname === "/api/v1/chat/sessions/chat-attachments/attachments" && request.method() === "GET") return json(route, {request_id: "attachments", attachments})
    if (url.pathname === "/api/v1/chat/sessions/chat-attachments/attachments" && request.method() === "POST") {
      const body = request.postDataJSON() as {filename: string; content_type: string; size: number}
      const attachment = {id: `attachment-image-${++attachmentSequence}`, session_id: "chat-attachments", filename: body.filename, content_type: body.content_type, size: body.size, sha256: "", status: "pending", parse_state: "pending", extraction_sha256: "", model_use_status: "not_used", rejection_code: null, message_id: null, created_at: 3, updated_at: 3}
      attachments = [...attachments, attachment]
      return json(route, {request_id: "reserve", attachment})
    }
    if (url.pathname.endsWith("/content") && request.method() === "PUT") {
      const attachmentId = url.pathname.split("/").at(-2)
      attachments = attachments.map((attachment) => attachment.id === attachmentId ? {...attachment, sha256: "a".repeat(64), status: "ready", parse_state: "ready", updated_at: 4} : attachment)
      return json(route, {request_id: "upload", attachment: attachments.find((attachment) => attachment.id === attachmentId)})
    }
    if (url.pathname.endsWith("/download")) return route.fulfill({status: 200, contentType: "image/png", body: image})
    if (url.pathname.includes("/attachments/") && request.method() === "DELETE") {
      const attachmentId = url.pathname.split("/").at(-1)
      attachments = attachments.filter((attachment) => attachment.id !== attachmentId)
      return json(route, {request_id: "delete"})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-attachments/messages" && request.method() === "POST") {
      const body = request.postDataJSON() as {content: string; attachment_ids?: string[]}
      sentAttachmentIds = body.attachment_ids ?? []
      attachments = attachments.map((attachment) => ({...attachment, message_id: "user-uploaded", model_use_status: "included"}))
      current = {...current, current_branch_head_id: "assistant-uploaded", message_count: 4, messages: [
        ...current.messages,
        {id: "user-uploaded", role: "user", status: "completed", content: body.content, parent_id: "assistant-details", reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], attachments, created_at: 5, updated_at: 5},
        {id: "assistant-uploaded", role: "assistant", status: "completed", content: "附件已处理。", parent_id: "user-uploaded", reply_to_id: "user-uploaded", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], attachments: [], created_at: 6, updated_at: 6},
      ]}
      return json(route, {request_id: "send", chat_session: current})
    }
    await route.continue()
  })
  return () => sentAttachmentIds
}

async function imageDataTransfer(page: Page, filename: string) {
  return page.evaluateHandle(({base64, name}) => {
    const bytes = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
    const transfer = new DataTransfer()
    transfer.items.add(new File([bytes], name, {type: "image/png"}))
    return transfer
  }, {base64: imagePngBase64, name: filename})
}

test("assistant-ui runtime 保留 Gateway 发送、重试和 SSE 刷新", async ({page}) => {
  await page.addInitScript(() => {
    const EventSourceBase = window.EventSource
    class TestEventSource extends EventTarget {
      readonly url: string
      readonly withCredentials = false
      readyState = 1
      constructor(url: string) {
        super()
        this.url = url
        setTimeout(() => this.dispatchEvent(new MessageEvent("chat", {data: "{}"})), 150)
      }
      close() { this.readyState = 2 }
    }
    window.EventSource = TestEventSource as unknown as typeof EventSourceBase
  })
  const sessionReads = await mockRuntimeGateway(page)
  await page.goto("/chat/chat-runtime")
  await expect(page.getByRole("heading", {name: "SSE 刷新后的会话", exact: true})).toBeVisible()
  await expect.poll(sessionReads).toBeGreaterThanOrEqual(2)
  await page.getByRole("button", {name: "重试", exact: true}).click()
  await expect(page.getByText("重试成功")).toBeVisible()
  await page.getByRole("textbox", {name: "输入消息"}).fill("新的问题")
  await page.getByRole("button", {name: "发送", exact: true}).click()
  await expect(page.getByText("发送成功")).toBeVisible()
})

test("ownership error 显示为中文且不泄露会话", async ({page}) => {
  const current = structuredClone(baseSession)
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === "/api/v1/actor") return json(route, {request_id: "actor", actor: {id: "user-other", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: [], is_platform_administrator: false}})
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions") return json(route, {request_id: "list", chat_sessions: [current]})
    if (url.pathname.endsWith("/attachments")) return json(route, {request_id: "attachments", attachments: []})
    if (url.pathname.endsWith("/events/stream")) return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    if (url.pathname === "/api/v1/chat/sessions/chat-private" && route.request().method() === "GET") return json(route, {request_id: "get", chat_session: current})
    if (url.pathname === "/api/v1/chat/sessions/chat-private/messages" && route.request().method() === "POST") return json(route, {request_id: "denied", error: {code: "chat_session_not_found", message: "not found"}}, 404)
    await route.continue()
  })
  await page.goto("/chat/chat-private")
  await page.getByRole("textbox", {name: "输入消息"}).fill("越权发送")
  await page.getByRole("button", {name: "发送", exact: true}).click()
  await expect(page.getByRole("alert")).toContainText("AI 对话会话不存在或无权访问。")
  await expect(page.getByText("越权发送")).toHaveCount(0)
})

test("附件、渐进披露和响应式会话栏", async ({page}, testInfo) => {
  const sentAttachments = await mockAttachmentGateway(page)
  await page.goto("/chat/chat-attachments")
  await expect(page.getByText("核心答案保持可见。")).toBeVisible()
  await expect(page.getByText("query_metrics")).toBeHidden()
  await page.getByText("查看分析详情").click()
  await expect(page.getByText("query_metrics")).toBeVisible()

  if (testInfo.project.name.startsWith("mobile")) {
    await page.getByRole("button", {name: "打开会话栏"}).click()
    await expect(page.getByLabel("搜索 AI 对话").last()).toBeVisible()
    await page.getByRole("button", {name: "关闭"}).click()
  } else {
    await page.getByRole("button", {name: "折叠会话栏"}).click()
    await expect(page.getByRole("button", {name: "展开会话栏"})).toBeVisible()
    await page.getByRole("button", {name: "展开会话栏"}).click()
  }

  const input = page.getByRole("textbox", {name: "输入消息"})
  const pendingAttachments = page.getByRole("list", {name: "待发送附件"})
  const dropped = await imageDataTransfer(page, "dropped.png")
  await input.locator("..").dispatchEvent("drop", {dataTransfer: dropped})
  await expect(pendingAttachments.getByText("dropped.png")).toBeVisible()
  await expect(pendingAttachments.getByText("已就绪")).toBeVisible()
  await page.getByRole("button", {name: "移除 dropped.png"}).click()
  await expect(pendingAttachments.getByText("dropped.png")).toHaveCount(0)

  await input.evaluate((element, {base64, name}) => {
    const bytes = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
    const event = new Event("paste", {bubbles: true, cancelable: true})
    Object.defineProperty(event, "clipboardData", {value: {files: [new File([bytes], name, {type: "image/png"})]}})
    element.dispatchEvent(event)
  }, {base64: imagePngBase64, name: "status.png"})
  await expect(pendingAttachments.getByText("status.png")).toBeVisible()
  await expect(pendingAttachments.getByText("已就绪")).toBeVisible()
  const thumbnail = pendingAttachments.locator("img")
  await expect(thumbnail).toBeVisible()
  expect(await thumbnail.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0)

  await input.fill("带附件的问题")
  await page.getByRole("button", {name: "发送", exact: true}).click()
  await expect(page.getByText("附件已处理。")).toBeVisible()
  expect(sentAttachments()).toEqual(["attachment-image-2"])
  await page.getByText("附件（1）").click()
  await expect(page.locator("article").filter({hasText: "带附件的问题"}).locator("img")).toBeVisible()

  const dimensions = await page.evaluate(() => ({viewport: window.innerWidth, page: document.documentElement.scrollWidth}))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({path: testInfo.outputPath("chat-attachments-responsive.png"), fullPage: true})
})
