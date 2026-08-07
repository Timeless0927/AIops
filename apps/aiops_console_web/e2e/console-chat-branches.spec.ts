import { expect, test, type Page, type Route } from "@playwright/test"

const baseSession = {
  id: "chat-branch",
  title: "分支排障",
  created_at: 1,
  updated_at: 2,
  expires_at: null,
  message_count: 2,
  selected_scope: null,
  pinned: false,
  archived: false,
  title_manual: false,
  current_branch_head_id: "assistant-original",
  event_cursor: 2,
  messages: [
    {id: "user-original", role: "user", status: "completed", content: "原问题", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 1, updated_at: 1},
    {id: "assistant-original", role: "assistant", status: "completed", content: "原回答", parent_id: "user-original", reply_to_id: "user-original", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], created_at: 2, updated_at: 2},
  ],
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockGateway(page: Page) {
  let current = structuredClone(baseSession)
  const branchTargets: string[] = []
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") return json(route, {request_id: "actor", actor: {id: "user-branch", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: [], is_platform_administrator: false}})
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") return json(route, {request_id: "list", chat_sessions: [current]})
    if (url.pathname === "/api/v1/chat/sessions/chat-branch" && request.method() === "GET") return json(route, {request_id: "get", chat_session: current})
    if (url.pathname === "/api/v1/chat/sessions/chat-branch/events/stream") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": reconnect\n\n"})
    if (url.pathname.endsWith("/edit") && request.method() === "POST") {
      const body = request.postDataJSON() as {content: string}
      current = {
        ...current,
        current_branch_head_id: "assistant-edited",
        messages: [
          ...current.messages.map((message) => ({...message, branch_count: message.role === "user" ? 2 : message.branch_count, is_current_branch: false})),
          {id: "user-edited", role: "user", status: "completed", content: body.content, parent_id: null, reply_to_id: null, branch_index: 2, branch_count: 2, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 3, updated_at: 3},
          {id: "assistant-edited", role: "assistant", status: "completed", content: "编辑后的回答", parent_id: "user-edited", reply_to_id: "user-edited", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], created_at: 4, updated_at: 4},
        ],
      }
      return json(route, {request_id: "edit", chat_session: current})
    }
    if (url.pathname.endsWith("/reload") && request.method() === "POST") {
      current = {
        ...current,
        current_branch_head_id: "assistant-reloaded",
        messages: current.messages.map((message) => ({
          ...message,
          branch_count: message.id === "assistant-edited" ? 2 : message.branch_count,
          is_current_branch: message.id === "user-edited",
        })),
      }
      current.messages.push({...current.messages.find((message) => message.id === "assistant-edited")!, id: "assistant-reloaded", content: "重新生成后的回答", branch_index: 2, branch_count: 2, is_current_branch: true, created_at: 5, updated_at: 5})
      return json(route, {request_id: "reload", chat_session: current})
    }
    if (url.pathname.endsWith("/branches") && request.method() === "POST") {
      const target = (request.postDataJSON() as {message_id: string}).message_id
      branchTargets.push(target)
      const visible = target === "assistant-original"
        ? new Set(["user-original", "assistant-original"])
        : target === "assistant-edited"
          ? new Set(["user-edited", "assistant-edited"])
          : new Set<string>()
      if (!visible.size) return json(route, {error: {code: "invalid_target", message: "unexpected branch target"}}, 400)
      current = {...current, current_branch_head_id: target, messages: current.messages.map((message) => ({...message, is_current_branch: visible.has(message.id)}))}
      return json(route, {request_id: "switch", chat_session: current})
    }
    await route.continue()
  })
  return branchTargets
}

test.describe("AI 对话消息分支", () => {
  test("编辑、重新生成和切换分支", async ({page}, testInfo) => {
    const branchTargets = await mockGateway(page)
    await page.goto("/chat/chat-branch")
    await expect(page.getByText("原回答")).toBeVisible()
    await page.getByRole("button", {name: "编辑"}).click()
    await page.getByRole("textbox", {name: "编辑消息"}).fill("修改后的问题")
    await page.getByRole("button", {name: "发送编辑"}).click()
    await expect(page.getByText("编辑后的回答")).toBeVisible()
    await page.getByRole("button", {name: "重新生成"}).click()
    await expect(page.getByText("重新生成后的回答")).toBeVisible()
    const regenerated = page.locator("article").filter({hasText: "重新生成后的回答"})
    await regenerated.getByRole("button", {name: "上一分支"}).click()
    await expect(page.getByText("编辑后的回答")).toBeVisible()
    expect(branchTargets).toEqual(["assistant-edited"])
    const editedQuestion = page.locator("article").filter({hasText: "修改后的问题"})
    await editedQuestion.getByRole("button", {name: "上一分支"}).click()
    await expect(page.getByText("原回答")).toBeVisible()
    expect(branchTargets).toEqual(["assistant-edited", "assistant-original"])
    await page.screenshot({path: testInfo.outputPath("chat-branches.png"), fullPage: true})
    const dimensions = await page.evaluate(() => ({viewport: window.innerWidth, page: document.documentElement.scrollWidth}))
    expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  })
})
