import { expect, test, type Page, type Route } from "@playwright/test"

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockLogin(page: Page) {
  let authenticated = false
  let logoutRequests = 0
  let logoutCsrf = ""
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
    if (url.pathname === "/auth/csrf") return json(route, {request_id: "csrf:t01", csrf_token: "csrf:t01"})
    if (url.pathname === "/auth/logout") {
      logoutCsrf = request.headers()["x-csrf-token"] ?? ""
      authenticated = false
      logoutRequests += 1
      return json(route, {request_id: "logout:t01"})
    }
    if (url.pathname === "/api/v1/incidents") return json(route, {request_id: "incidents:t01", incidents: []})
    if (url.pathname === "/api/v1/chat/sessions") return json(route, {request_id: "chat:t01", chat_sessions: []})
    if (url.pathname === "/api/v1/resources") return json(route, {request_id: "resources:t01", resources: []})
    await route.continue()
  })
  return {logoutRequests: () => logoutRequests, logoutCsrf: () => logoutCsrf}
}

test("user menu opens without page errors and logs out", async ({page}, testInfo) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  const session = await mockLogin(page)
  await page.goto("/login")
  await page.getByLabel("用户名").fill("operator")
  await page.getByLabel("密码").fill("test-password")
  await page.getByRole("button", {name: "登录"}).click()

  if (testInfo.project.name.startsWith("mobile")) {
    await page.getByRole("button", {name: "切换侧栏"}).click()
  }
  await page.getByRole("button", {name: /值班工程师/}).click()
  await expect(page.getByRole("menuitem", {name: "退出登录"})).toBeVisible()
  expect(pageErrors).toEqual([])

  await page.getByRole("menuitem", {name: "退出登录"}).click()
  await expect.poll(session.logoutRequests).toBe(1)
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole("heading", {name: "登录"})).toBeVisible()
  expect(session.logoutCsrf()).toBe("csrf:t01")
  expect(pageErrors).toEqual([])
})

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
  await expect(page.getByRole("region", {name: "AI 对话消息"}).getByText("尚无 AI 对话会话")).toBeVisible()

  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({path: testInfo.outputPath("console-shell.png"), fullPage: true})
})

test("theme controls remain usable when browser storage is unavailable", async ({page}) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  await page.addInitScript(() => {
    Object.defineProperty(Storage.prototype, "getItem", {value: () => { throw new DOMException("blocked", "SecurityError") }})
    Object.defineProperty(Storage.prototype, "setItem", {value: () => { throw new DOMException("blocked", "SecurityError") }})
  })
  await mockLogin(page)
  await page.goto("/login")
  await page.getByLabel("用户名").fill("operator")
  await page.getByLabel("密码").fill("test-password")
  await page.getByRole("button", {name: "登录"}).click()

  await page.getByRole("button", {name: "切换为浅色主题"}).click()
  await expect(page.getByRole("button", {name: "切换为深色主题"})).toBeVisible()
  expect(pageErrors).toEqual([])
})
