import { expect, test, type Page, type Route } from "@playwright/test"

const session = {
  id: "chat-t02",
  title: "checkout 错误率排查",
  created_at: 1,
  updated_at: 2,
  expires_at: null,
  message_count: 2,
  selected_scope: null,
  pinned: false,
  archived: false,
  title_manual: false,
  event_cursor: 2,
  messages: [
    {id: "m1", role: "user", status: "completed", content: "checkout 错误率？", reply_to_id: null, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 1, updated_at: 1},
    {id: "m2", role: "assistant", status: "completed", content: "当前错误率正常。", reply_to_id: "m1", error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], created_at: 2, updated_at: 2},
  ],
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockGateway(page: Page) {
  let authenticated = false
  let current = structuredClone(session)
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") {
      return authenticated
        ? json(route, {request_id: "actor:t02", actor: {id: "user:t02", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: [], is_platform_administrator: false}})
        : json(route, {request_id: "actor:t02", error: {code: "unauthorized", message: "请先登录"}}, 401)
    }
    if (url.pathname === "/auth/login") {
      authenticated = true
      return json(route, {request_id: "login:t02", actor_id: "user:t02"})
    }
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf:t02", csrf_token: "csrf:t02"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents:t02", incidents: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources:t02", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") {
      const query = url.searchParams.get("query") ?? ""
      const filter = url.searchParams.get("filter") ?? "all"
      const matches = query ? current.title.includes(query) || current.messages.some((message) => message.content.includes(query)) : true
      const visible = filter === "archived" ? current.archived : !current.archived && (filter !== "pinned" || current.pinned) && (filter !== "normal" || !current.pinned)
      return json(route, {request_id: "chat-list:t02", chat_sessions: matches && visible ? [current] : []})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t02" && request.method() === "GET") {
      return current.archived ? json(route, {request_id: "chat:get:t02", chat_session: current}) : json(route, {request_id: "chat:get:t02", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t02/attachments") return json(route, {request_id: "attachments:t02", attachments: []})
    if (url.pathname === "/api/v1/chat/sessions/chat-t02" && request.method() === "PATCH") {
      const body = request.postDataJSON() as {title?: string; pinned?: boolean; archived?: boolean}
      current = {...current, ...(body.title ? {title: body.title, title_manual: true} : {}), ...(typeof body.pinned === "boolean" ? {pinned: body.pinned} : {}), ...(typeof body.archived === "boolean" ? {archived: body.archived, pinned: body.archived ? false : current.pinned} : {})}
      return json(route, {request_id: "chat:update:t02", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t02" && request.method() === "DELETE") {
      current = {...current, archived: true}
      return json(route, {request_id: "chat:delete:t02", chat_session_id: "chat-t02", deleted: true})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t02/events/stream") {
      return route.fulfill({status: 200, contentType: "text/event-stream", body: ": reconnect\n\n"})
    }
    await route.continue()
  })
}

test("AI 对话会话管理主流程", async ({page}, testInfo) => {
  await mockGateway(page)
  await page.goto("/login")
  await page.getByLabel("用户名").fill("operator")
  await page.getByLabel("密码").fill("test-password")
  await page.getByRole("button", {name: "登录"}).click()
  await page.goto("/chat/chat-t02")
  await expect(page.getByRole("heading", {name: "checkout 错误率排查", exact: true})).toBeVisible()
  if (testInfo.project.name.startsWith("mobile")) await page.getByRole("button", {name: "打开会话栏"}).click()

  await page.getByRole("button", {name: "操作 checkout 错误率排查"}).click()
  await page.getByRole("menuitem", {name: "重命名"}).click()
  await page.getByRole("textbox", {name: "重命名 AI 对话"}).fill("支付服务排障")
  await page.getByRole("textbox", {name: "重命名 AI 对话"}).press("Enter")
  await expect(page.getByText("支付服务排障", {exact: true}).last()).toBeVisible()

  await page.getByRole("button", {name: "操作 支付服务排障"}).click()
  await page.getByRole("menuitem", {name: "置顶"}).click()
  await expect(page.getByLabel("已置顶").last()).toBeVisible()

  await page.getByLabel("搜索 AI 对话").last().fill("checkout")
  await expect(page.getByText("支付服务排障", {exact: true}).last()).toBeVisible()

  await page.getByRole("button", {name: "操作 支付服务排障"}).click()
  await page.getByRole("menuitem", {name: "归档会话"}).click()
  await page.getByRole("combobox", {name: "筛选 AI 对话"}).click()
  await page.getByRole("option", {name: "已归档"}).click()
  await expect(page.getByText("支付服务排障", {exact: true}).last()).toBeVisible()

  await page.getByRole("button", {name: "操作 支付服务排障"}).click()
  await page.getByRole("menuitem", {name: "恢复会话"}).click()
  await page.getByRole("combobox", {name: "筛选 AI 对话"}).click()
  await page.getByRole("option", {name: "正常会话"}).click()
  await expect(page.getByText("支付服务排障", {exact: true}).last()).toBeVisible()

  await page.getByRole("button", {name: "操作 支付服务排障"}).click()
  await page.getByRole("menuitem", {name: "永久删除"}).click()
  await expect(page.getByRole("alertdialog")).toContainText("无法恢复")
  await page.getByRole("button", {name: "永久删除"}).last().click()
  await expect(page).toHaveURL(/\/chat$/)

  const dimensions = await page.evaluate(() => ({viewport: window.innerWidth, page: document.documentElement.scrollWidth}))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({path: testInfo.outputPath("chat-management.png"), fullPage: true})
})
