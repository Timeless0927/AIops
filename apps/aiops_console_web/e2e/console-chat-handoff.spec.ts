import { expect, test, type Page, type Route } from "@playwright/test"

const sessionTemplate = {
  id: "chat-t09", title: "新建 AI 对话", created_at: 1, updated_at: 1, expires_at: null,
  message_count: 0, selected_scope: null, pinned: false, archived: false, title_manual: false,
  current_branch_head_id: null, event_cursor: 0, messages: [],
}

const incident = {
  id: "incident-t09", title: "checkout 发布异常", severity: "critical", status: "active",
  lifecycle_state: "firing", binding_status: "bound", origin: "alert", cluster_id: "cluster-prod",
  cluster_name: "生产集群", environment: "prod", namespace: "shop", alertname: "HighErrorRate",
  workload_name: "checkout-api", service_name: "checkout", team_name: "Payments", signal_count: 1,
  diagnosis_outcome: null, evidence_gate_status: null, evidence_revision: 0,
  resolved_at: null, reopened_at: null, created_at: 1, updated_at: 2,
}

const handoffEvent = {
  id: 2, investigation_id: "investigation-t09", type: "human_input.assertion", actor_id: "user-t09",
  payload: {
    content: "带附件的问题", source: "chat_handoff", content_sha256: "a".repeat(64),
    attachments: [{
      source_attachment_id: "attachment-t09",
      retained_reference_id: "handoff:handoff-t09:attachment:attachment-t09",
      filename: "incident.log", content_type: "text/plain", size: 24, sha256: "b".repeat(64),
    }],
  },
  created_at: 2,
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockGateway(page: Page) {
  let authenticated = false
  let created = false
  let current: any = structuredClone(sessionTemplate)
  let attachments: any[] = []
  let handoffCount = 0
  let eventCursor = 2

  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") {
      return authenticated
        ? json(route, {request_id: "actor", actor: {id: "user-t09", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: ["manage_investigation", "view_incident"], is_platform_administrator: false}})
        : json(route, {request_id: "actor", error: {code: "unauthorized", message: "请先登录"}}, 401)
    }
    if (url.pathname === "/auth/login") {
      authenticated = true
      return json(route, {request_id: "login", actor_id: "user-t09"})
    }
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf-t09"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: [incident]})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "POST") {
      created = true
      return json(route, {request_id: "create", chat_session: current}, 201)
    }
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") {
      const query = url.searchParams.get("query") ?? ""
      const filter = url.searchParams.get("filter") ?? "all"
      const matches = !query || current.title.includes(query) || current.messages.some((message: any) => String(message.content).includes(query))
      const visible = filter === "archived"
        ? current.archived
        : !current.archived && (filter !== "pinned" || current.pinned) && (filter !== "normal" || !current.pinned)
      return json(route, {request_id: "chats", chat_sessions: created && matches && visible ? [current] : []})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09" && request.method() === "GET") {
      return json(route, {request_id: "chat", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09" && request.method() === "PATCH") {
      const body = request.postDataJSON() as {title?: string; pinned?: boolean; archived?: boolean}
      current = {
        ...current,
        ...(body.title ? {title: body.title, title_manual: true} : {}),
        ...(typeof body.pinned === "boolean" ? {pinned: body.pinned} : {}),
        ...(typeof body.archived === "boolean" ? {archived: body.archived, pinned: body.archived ? false : current.pinned} : {}),
      }
      return json(route, {request_id: "update", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/attachments" && request.method() === "GET") {
      return json(route, {request_id: "attachments", attachments})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/attachments" && request.method() === "POST") {
      const body = request.postDataJSON() as {filename: string; content_type: string; size: number}
      const attachment = {
        id: "attachment-t09", session_id: "chat-t09", filename: body.filename,
        content_type: body.content_type, size: body.size, sha256: "", status: "pending",
        parse_state: "pending", extraction_sha256: "", model_use_status: "not_used",
        rejection_code: null, message_id: null, created_at: 2, updated_at: 2,
      }
      attachments = [attachment]
      return json(route, {request_id: "reserve", attachment})
    }
    if (url.pathname.endsWith("/attachment-t09/content") && request.method() === "PUT") {
      attachments = attachments.map((attachment) => ({
        ...attachment, sha256: "b".repeat(64), status: "ready", parse_state: "ready",
        extraction_sha256: "c".repeat(64), updated_at: 3,
      }))
      return json(route, {request_id: "upload", attachment: attachments[0]})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/messages" && request.method() === "POST") {
      const body = request.postDataJSON() as {content: string; attachment_ids?: string[]}
      expect(body.attachment_ids).toEqual(["attachment-t09"])
      attachments = attachments.map((attachment) => ({...attachment, message_id: "user-t09", model_use_status: "included"}))
      current = {
        ...current, updated_at: 4, message_count: 2, current_branch_head_id: "assistant-t09", event_cursor: 2,
        messages: [
          {id: "user-t09", role: "user", status: "completed", content: body.content, parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], attachments, created_at: 3, updated_at: 3},
          {id: "assistant-t09", role: "assistant", status: "completed", content: "建议转交正式调查。", parent_id: "user-t09", reply_to_id: "user-t09", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: "关联 Incident 继续核实。", completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], attachments: [], created_at: 4, updated_at: 4},
        ],
      }
      return json(route, {request_id: "send", chat_session: current})
    }
    if (url.pathname.endsWith("/assistant-t09/reload") && request.method() === "POST") {
      const [userMessage, assistantMessage] = current.messages
      current = {
        ...current, updated_at: 5, current_branch_head_id: "assistant-t09-v2", event_cursor: 3,
        messages: [
          {...userMessage, is_current_branch: true},
          {...assistantMessage, branch_count: 2, is_current_branch: false},
          {...assistantMessage, id: "assistant-t09-v2", content: "重新生成的调查建议。", branch_index: 2, branch_count: 2, is_current_branch: true, created_at: 5, updated_at: 5},
        ],
      }
      return json(route, {request_id: "reload", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/branches" && request.method() === "POST") {
      expect(request.postDataJSON()).toMatchObject({message_id: "assistant-t09"})
      current = {
        ...current, current_branch_head_id: "assistant-t09",
        messages: current.messages.map((message: any) => ({...message, is_current_branch: message.id === "user-t09" || message.id === "assistant-t09"})),
      }
      return json(route, {request_id: "branch", chat_session: current})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/events/stream") {
      return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    }
    if (url.pathname === "/api/v1/chat/sessions/chat-t09/handoffs" && request.method() === "POST") {
      expect(request.postDataJSON()).toMatchObject({message_ids: ["user-t09"], target: {type: "existing_incident", incident_id: "incident-t09"}})
      handoffCount += 1
      return json(route, {request_id: `handoff-${handoffCount}`, handoff: {id: "handoff-t09", chat_session_id: "chat-t09", target_type: "existing_incident", incident_id: "incident-t09", investigation_id: "investigation-t09", selected_message_ids: ["user-t09"], created_at: 6, idempotent: handoffCount > 1}}, handoffCount > 1 ? 200 : 201)
    }
    if (url.pathname === "/api/v1/incidents/incident-t09/workbench") return json(route, {
      request_id: "workbench", incident,
      resource_context: {cluster_id: "cluster-prod", cluster_name: "生产集群", environment: "prod", namespace: "shop", runtime_status: "available", deployment_target_id: "target-t09", workload_kind: "Deployment", workload_name: "checkout-api", service_id: "service-t09", service_name: "checkout", team_id: "team-t09", team_name: "Payments"},
      alert_signals: [], investigation: {id: "investigation-t09", incident_id: "incident-t09", sequence: 1, status: "running", started_at: 1, completed_at: null, created_at: 1, updated_at: 2},
      evidence_steps: [], judgment: null, recommended_actions: [], change_requests: [], recovery_observation: null,
      responsibility: {team_id: "team-t09", team_name: "Payments", service_id: "service-t09", service_name: "checkout"},
      actor_capabilities: ["manage_investigation", "view_incident"], snapshot_revision: "1", event_cursor: eventCursor,
    })
    if (url.pathname === "/api/v1/investigations/investigation-t09/events") return json(route, {request_id: "events", events: [handoffEvent], next_cursor: eventCursor, has_more: false})
    if (url.pathname === "/api/v1/investigations/investigation-t09/events/stream") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    if (url.pathname === "/api/v1/investigations/investigation-t09/human-input" && request.method() === "POST") {
      const body = request.postDataJSON() as {kind: string; content: string}
      eventCursor += 1
      return json(route, {request_id: "feedback", event: {id: eventCursor, investigation_id: "investigation-t09", type: `human_input.${body.kind}`, actor_id: "user-t09", payload: {content: body.content}, created_at: 7}})
    }
    await route.continue()
  })
}

test("AI 对话固定端到端验收流程", async ({page}, testInfo) => {
  const mobile = testInfo.project.name.startsWith("mobile")
  await mockGateway(page)

  await page.goto("/login")
  await page.getByLabel("用户名").fill("operator")
  await page.getByLabel("密码").fill("test-password")
  await page.getByRole("button", {name: "登录"}).click()
  await expect(page).toHaveURL(/\/incidents$/)
  if (mobile) await page.getByRole("button", {name: "切换侧栏"}).click()
  await page.getByRole("link", {name: "AI 对话"}).click()
  await expect(page).toHaveURL(/\/chat$/)

  if (mobile) await page.getByRole("button", {name: "打开会话栏"}).click()
  await page.getByRole("button", {name: "新建对话"}).last().click()
  await expect(page).toHaveURL(/\/chat\/chat-t09$/)
  await expect(page.getByRole("heading", {name: "新建 AI 对话", exact: true})).toBeVisible()
  if (mobile) await page.getByRole("button", {name: "打开会话栏"}).click()

  await page.getByRole("button", {name: "操作 新建 AI 对话"}).click()
  await page.getByRole("menuitem", {name: "重命名"}).click()
  await page.getByRole("textbox", {name: "重命名 AI 对话"}).fill("发布异常排查")
  await page.getByRole("textbox", {name: "重命名 AI 对话"}).press("Enter")
  await page.getByRole("button", {name: "操作 发布异常排查"}).click()
  await page.getByRole("menuitem", {name: "置顶"}).click()
  await expect(page.getByLabel("已置顶").last()).toBeVisible()
  await page.getByLabel("搜索 AI 对话").last().fill("发布")
  await expect(page.getByText("发布异常排查", {exact: true}).last()).toBeVisible()

  await page.getByRole("button", {name: "操作 发布异常排查"}).click()
  await page.getByRole("menuitem", {name: "归档会话"}).click()
  await page.getByRole("combobox", {name: "筛选 AI 对话"}).click()
  await page.getByRole("option", {name: "已归档"}).click()
  await page.getByRole("button", {name: "操作 发布异常排查"}).click()
  await page.getByRole("menuitem", {name: "恢复会话"}).click()
  await page.getByRole("combobox", {name: "筛选 AI 对话"}).click()
  await page.getByRole("option", {name: "正常会话"}).click()
  await expect(page.getByText("发布异常排查", {exact: true}).last()).toBeVisible()
  if (mobile) await page.getByRole("button", {name: "关闭"}).click()

  const fileChooser = page.waitForEvent("filechooser")
  await page.getByRole("button", {name: "选择附件"}).click()
  await (await fileChooser).setFiles({name: "incident.log", mimeType: "text/plain", buffer: Buffer.from("upstream timeout observed")})
  await expect(page.getByRole("list", {name: "待发送附件"}).getByText("已就绪")).toBeVisible()
  await page.getByRole("textbox", {name: "输入消息"}).fill("带附件的问题")
  await page.getByRole("button", {name: "发送", exact: true}).click()
  await expect(page.getByText("建议转交正式调查。")).toBeVisible()

  await page.getByRole("button", {name: "重新生成"}).click()
  await expect(page.getByText("重新生成的调查建议。")).toBeVisible()
  await page.locator("article").filter({hasText: "重新生成的调查建议。"}).getByRole("button", {name: "上一分支"}).click()
  await expect(page.getByText("建议转交正式调查。")).toBeVisible()

  await page.getByRole("checkbox", {name: "选择消息 带附件的问题"}).click()
  await expect(page.getByText("已选择 1 条消息和 1 个附件")).toBeVisible()
  await page.getByText("核对转交内容").click()
  await expect(page.getByRole("button", {name: "确认转交"})).toBeEnabled()
  await page.getByRole("button", {name: "确认转交"}).click()
  await page.getByRole("button", {name: "留在 AI 对话"}).click()
  await page.getByRole("button", {name: "确认转交"}).click()
  await expect(page.getByText("重复请求已安全返回相同结果")).toBeVisible()
  await page.getByRole("button", {name: "进入事件调查"}).click()

  await expect(page).toHaveURL(/\/incidents\/incident-t09$/)
  await expect(page.getByRole("heading", {name: "checkout 发布异常"})).toBeVisible()
  await expect(page.getByRole("heading", {name: "决策轨迹（Decision Trace）"})).toBeVisible()
  await expect(page.getByRole("heading", {name: "建议动作（Recommended Actions）"})).toBeVisible()
  await expect(page.getByRole("heading", {name: "告警信号（Alert Signals）"})).toBeVisible()
  await expect(page.getByText("敏感输入（Secure Input）", {exact: true})).toBeVisible()
  await expect(page.getByText("键名（Key name）", {exact: true})).toBeVisible()
  await expect(page.getByText("期望结果", {exact: true})).toBeVisible()
  await expect(page.getByText("背景信息", {exact: true})).toBeVisible()
  await page.keyboard.press("Tab")
  await expect(page.locator(":focus-visible")).toBeVisible()

  const dimensions = await page.evaluate(() => ({viewport: window.innerWidth, page: document.documentElement.scrollWidth}))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({path: testInfo.outputPath("chat-t09-acceptance.png"), fullPage: true})
})
