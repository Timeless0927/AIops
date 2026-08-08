import { expect, test, type Page, type Route } from "@playwright/test"

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
