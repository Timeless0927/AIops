import { expect, test, type Page, type Route } from "@playwright/test"

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockLogin(page: Page) {
  let authenticated = false
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === "/api/v1/actor") {
      return authenticated
        ? json(route, {request_id: "actor:t01", actor: {
            id: "user:t01", username: "operator", display_name: "值班工程师",
            roles: ["sre"], capabilities: [], is_platform_administrator: false,
          }})
        : json(route, {request_id: "actor:t01", error: {code: "unauthorized", message: "请先登录"}}, 401)
    }
    if (url.pathname === "/auth/login") {
      expect(request.postDataJSON()).toEqual({username: "operator", password: "test-password", session_mode: "cookie"})
      authenticated = true
      return json(route, {request_id: "login:t01", actor_id: "user:t01"})
    }
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents:t01", incidents: []})
    if (url.pathname === "/api/v1/chat/sessions") return json(route, {request_id: "chat:t01", chat_sessions: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources:t01", resources: []})
    await route.continue()
  })
}

test("login enters the permission-aware Chinese Console shell and AI dialogue", async ({page}, testInfo) => {
  await mockLogin(page)
  await page.goto("/login")

  await expect(page.getByRole("heading", {name: "登录"})).toBeVisible()
  await page.getByLabel("用户名").fill("operator")
  await page.getByLabel("密码").fill("test-password")
  await page.getByRole("button", {name: "登录"}).click()
  await expect(page).toHaveURL(/\/incidents$/)

  if (testInfo.project.name.startsWith("mobile")) {
    await page.getByRole("button", {name: "切换侧栏"}).click()
  }
  await expect(page.locator('a[data-sidebar="menu-button"][href="/chat"]')).toBeVisible()
  await expect(page.locator('a[data-sidebar="menu-button"][href="/incidents"]')).toBeVisible()
  await expect(page.locator('a[data-sidebar="menu-button"][href="/reports"]')).toBeVisible()
  await expect(page.locator('a[data-sidebar="menu-button"][href="/admin"]')).toHaveCount(0)
  await expect(page.locator("body")).not.toContainText("Acme")
  await expect(page.locator('a[href="#"]')).toHaveCount(0)

  await page.locator('a[data-sidebar="menu-button"][href="/chat"]').click()
  await expect(page).toHaveURL(/\/chat$/)
  await expect(page.getByRole("heading", {name: "AI 对话", exact: true})).toBeVisible()
  await expect(page.getByText("尚无 AI 对话会话")).toBeVisible()

  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({path: testInfo.outputPath("console-shell.png"), fullPage: true})
})
