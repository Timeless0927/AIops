import { expect, test, type Page, type Route } from "@playwright/test"

const attachment = {
  id: "attachment-t08", session_id: "chat-t08", filename: "incident.log", content_type: "text/plain",
  size: 42, sha256: "b".repeat(64), status: "ready", parse_state: "ready",
  extraction_sha256: "c".repeat(64), model_use_status: "included", rejection_code: null,
  message_id: "user-t08", created_at: 1, updated_at: 2,
}

const chatSession = {
  id: "chat-t08", title: "发布后错误率", created_at: 1, updated_at: 2, expires_at: null,
  message_count: 2, selected_scope: null, pinned: false, archived: false, title_manual: false,
  current_branch_head_id: "assistant-t08", event_cursor: 2,
  messages: [
    {id: "user-t08", role: "user", status: "completed", content: "发布后错误率升高", parent_id: null, reply_to_id: null, branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: null, completion: null, skill_versions: [], attachments: [attachment], created_at: 1, updated_at: 1},
    {id: "assistant-t08", role: "assistant", status: "completed", content: "建议转交正式调查。", parent_id: "user-t08", reply_to_id: "user-t08", branch_index: 1, branch_count: 1, is_current_branch: true, error_code: null, mode: "knowledge", scope: null, tool_activity: [], evidence_references: [], uncertainty: null, next_step: "关联 Incident 继续核实。", completion: {status: "completed", stopping_reason: "knowledge_answered"}, skill_versions: [], attachments: [], created_at: 2, updated_at: 2},
  ],
}

const incident = {
  id: "incident-t08", title: "checkout 发布异常", severity: "critical", status: "active",
  lifecycle_state: "firing", binding_status: "bound", origin: "alert", cluster_id: "cluster-prod",
  cluster_name: "生产集群", environment: "prod", namespace: "shop", alertname: "HighErrorRate",
  workload_name: "checkout-api", service_name: "checkout", team_name: "Payments", signal_count: 1,
  diagnosis_outcome: null, evidence_gate_status: null, evidence_revision: 0,
  resolved_at: null, reopened_at: null, created_at: 1, updated_at: 2,
}

const handoffEvent = {
  id: 2, investigation_id: "investigation-t08", type: "human_input.assertion", actor_id: "user-t08",
  payload: {
    content: "发布后错误率升高", source: "chat_handoff", content_sha256: "a".repeat(64),
    attachments: [{
      source_attachment_id: "attachment-t08",
      retained_reference_id: "handoff:handoff-t08:attachment:attachment-t08",
      filename: "incident.log", content_type: "text/plain", size: 42, sha256: "b".repeat(64),
    }],
  },
  created_at: 2,
}

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockGateway(page: Page) {
  let handoffCount = 0
  let eventCursor = 2
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") return json(route, {request_id: "actor", actor: {id: "user-t08", username: "operator", display_name: "值班工程师", roles: ["sre"], capabilities: ["manage_investigation", "view_incident"], is_platform_administrator: false}})
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf", csrf_token: "csrf"})
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents", incidents: [incident]})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources", resources: []})
    if (url.pathname === "/api/v1/chat/sessions" && request.method() === "GET") return json(route, {request_id: "chats", chat_sessions: [chatSession]})
    if (url.pathname === "/api/v1/chat/sessions/chat-t08" && request.method() === "GET") return json(route, {request_id: "chat", chat_session: chatSession})
    if (url.pathname === "/api/v1/chat/sessions/chat-t08/attachments") return json(route, {request_id: "attachments", attachments: [attachment]})
    if (url.pathname === "/api/v1/chat/sessions/chat-t08/events/stream") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    if (url.pathname === "/api/v1/chat/sessions/chat-t08/handoffs" && request.method() === "POST") {
      expect(request.postDataJSON()).toMatchObject({message_ids: ["user-t08"], target: {type: "existing_incident", incident_id: "incident-t08"}})
      handoffCount += 1
      return json(route, {request_id: `handoff-${handoffCount}`, handoff: {id: "handoff-t08", chat_session_id: "chat-t08", target_type: "existing_incident", incident_id: "incident-t08", investigation_id: "investigation-t08", selected_message_ids: ["user-t08"], created_at: 3, idempotent: handoffCount > 1}}, handoffCount > 1 ? 200 : 201)
    }
    if (url.pathname === "/api/v1/incidents/incident-t08/workbench") return json(route, {
      request_id: "workbench", incident,
      resource_context: {cluster_id: "cluster-prod", cluster_name: "生产集群", environment: "prod", namespace: "shop", runtime_status: "available", deployment_target_id: "target-t08", workload_kind: "Deployment", workload_name: "checkout-api", service_id: "service-t08", service_name: "checkout", team_id: "team-t08", team_name: "Payments"},
      alert_signals: [], investigation: {id: "investigation-t08", incident_id: "incident-t08", sequence: 1, status: "running", started_at: 1, completed_at: null, created_at: 1, updated_at: 2},
      evidence_steps: [], judgment: null, recommended_actions: [], change_requests: [], recovery_observation: null,
      responsibility: {team_id: "team-t08", team_name: "Payments", service_id: "service-t08", service_name: "checkout"},
      actor_capabilities: ["manage_investigation", "view_incident"], snapshot_revision: "1", event_cursor: eventCursor,
    })
    if (url.pathname === "/api/v1/investigations/investigation-t08/events") return json(route, {request_id: "events", events: [handoffEvent], next_cursor: eventCursor, has_more: false})
    if (url.pathname === "/api/v1/investigations/investigation-t08/events/stream") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": keepalive\n\n"})
    if (url.pathname === "/api/v1/investigations/investigation-t08/human-input" && request.method() === "POST") {
      const body = request.postDataJSON() as {kind: string; content: string}
      eventCursor += 1
      return json(route, {request_id: "feedback", event: {id: eventCursor, investigation_id: "investigation-t08", type: `human_input.${body.kind}`, actor_id: "user-t08", payload: {content: body.content}, created_at: 4}})
    }
    if (url.pathname === "/api/v1/investigations/investigation-t08/controls" && request.method() === "POST") {
      eventCursor += 1
      return json(route, {request_id: "control", event: {id: eventCursor, investigation_id: "investigation-t08", type: "investigation.lifecycle", actor_id: "user-t08", payload: {from: "running", to: "terminated"}, created_at: 5}})
    }
    await route.continue()
  })
}

test.describe("AI 对话 Handoff 与事件调查反馈", () => {
  test("显式转交、选择去留并渐进披露调查反馈与控制", async ({page}, testInfo) => {
    await mockGateway(page)
    await page.goto("/chat/chat-t08")

    await page.getByRole("checkbox", {name: "选择消息 发布后错误率升高"}).click()
    await expect(page.getByText("已选择 1 条消息和 1 个附件")).toBeVisible()
    await page.getByText("核对转交内容").click()
    await page.getByRole("button", {name: "确认转交"}).click()
    await expect(page.getByRole("dialog", {name: "已转交事件调查"})).toBeVisible()
    await page.getByRole("button", {name: "留在 AI 对话"}).click()
    await expect(page).toHaveURL(/\/chat\/chat-t08$/)

    await page.getByRole("button", {name: "确认转交"}).click()
    await expect(page.getByText("重复请求已安全返回相同结果")).toBeVisible()
    await page.getByRole("button", {name: "进入事件调查"}).click()
    await expect(page).toHaveURL(/\/incidents\/incident-t08$/)
    await expect(page.getByRole("heading", {name: "checkout 发布异常"})).toBeVisible()

    const eventTrigger = page.getByRole("button", {name: /人工输入.*发布后错误率升高/})
    await expect(page.getByText("incident.log")).toBeHidden()
    await eventTrigger.click()
    await expect(page.getByText("incident.log")).toBeVisible()
    await page.getByRole("button", {name: "修正此输入"}).click()
    await expect(page.getByRole("dialog", {name: "修正输入 #2"})).toBeVisible()
    await page.getByRole("button", {name: "取消"}).click()

    await page.getByRole("button", {name: "提供反馈"}).click()
    await page.getByRole("textbox", {name: "Human Input"}).fill("补充反馈：仅在生产环境复现")
    await page.getByRole("button", {name: "提交反馈"}).click()
    await expect(page.getByText("补充反馈：仅在生产环境复现")).toBeVisible()

    await page.getByRole("button", {name: "调查控制"}).click()
    await expect(page.getByRole("dialog", {name: "调查控制"})).toBeVisible()
    await page.getByRole("button", {name: "终止调查"}).click()
    await expect(page.getByRole("alertdialog", {name: "终止当前调查？"})).toBeVisible()
    await page.getByRole("button", {name: "取消"}).click()

    const dimensions = await page.evaluate(() => ({viewport: window.innerWidth, page: document.documentElement.scrollWidth}))
    expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
    await page.screenshot({path: testInfo.outputPath("chat-handoff-workbench.png"), fullPage: true})
  })
})
