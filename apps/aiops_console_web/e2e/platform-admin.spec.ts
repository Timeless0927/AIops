import { expect, test, type Page, type Route } from "@playwright/test"

async function json(route: Route, body: object, status = 200) {
  await route.fulfill({status, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockPlatformAdmin(page: Page) {
  const mutationReasons: string[] = []
  let reauthRequests = 0
  await page.route("**/*", async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === "/api/v1/actor") return json(route, {request_id: "actor:admin", actor: {
      id: "user:admin", username: "admin", display_name: "平台管理员",
      roles: ["platform_administrator"], capabilities: [], is_platform_administrator: true,
    }})
    if (path === "/auth/csrf") return json(route, {request_id: "csrf:admin", csrf_token: "csrf:admin"})
    if (path === "/auth/reauth") {
      reauthRequests += 1
      expect(request.postDataJSON()).toEqual({password: "current-password"})
      return json(route, {request_id: "reauth:admin"})
    }
    if (path === "/api/v1/admin/users" && request.method() === "POST") {
      mutationReasons.push(request.postDataJSON().reason)
      if (mutationReasons.length === 1) {
        return json(route, {request_id: "users:fresh", error: {code: "fresh_auth_required", message: "请重新认证"}}, 401)
      }
      return json(route, {request_id: "users:created"})
    }
    if (path === "/api/v1/admin/users") return json(route, {
      request_id: "users:list",
      users: [{id: "user:existing", username: "operator", display_name: "值班工程师", active: true, created_at: 1, updated_at: 1}],
      teams: [], team_memberships: [], role_bindings: [],
    })
    if (path === "/api/v1/admin/connector-enrollments") return json(route, {request_id: "connectors:list", connector_enrollments: [], clusters: []})
    if (path === "/api/v1/admin/resource-catalog") return json(route, {request_id: "catalog:list", discovery_candidates: [], services: [], deployment_targets: [], resource_bindings: []})
    await route.continue()
  })
  return {mutationReasons, reauthRequests: () => reauthRequests}
}

test("platform management is responsive and governs each mutation in its action dialog", async ({page}, testInfo) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  const requests = await mockPlatformAdmin(page)
  await page.goto("/admin?section=users")

  await expect(page.getByRole("heading", {name: "用户", exact: true})).toBeVisible()
  await expect(page.getByLabel("变更原因")).toHaveCount(0)
  await expect(page.getByLabel("当前密码")).toHaveCount(0)
  if (testInfo.project.name.startsWith("mobile")) {
    await expect(page.getByLabel("选择管理分区")).toBeVisible()
    await expect(page.getByLabel("选择管理分区").locator('[data-slot="select-value"]')).toHaveText("身份与权限")
    await expect(page.getByRole("navigation", {name: "管理分区"})).toBeHidden()
  } else {
    await expect(page.getByRole("navigation", {name: "管理分区"})).toBeVisible()
    await expect(page.getByLabel("选择管理分区")).toBeHidden()
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.getByRole("button", {name: /新建用户/}).click()
  await page.getByLabel("用户名").fill("new-operator")
  await page.getByLabel("显示名称").fill("新值班工程师")
  await page.getByLabel("初始密码").fill("initial-password")
  await page.getByRole("button", {name: "创建用户"}).click()

  const dialog = page.getByRole("dialog", {name: "创建用户"})
  await expect(dialog).toBeVisible()
  await dialog.getByLabel("变更原因").fill("新增轮值成员")
  await dialog.getByRole("button", {name: "确认执行"}).click()
  await expect(dialog.getByLabel("当前密码")).toBeVisible()
  await expect(dialog.getByLabel("变更原因")).toHaveValue("新增轮值成员")
  await dialog.getByLabel("当前密码").fill("current-password")
  await dialog.getByRole("button", {name: "重新认证并重试"}).click()
  await expect(dialog).toBeHidden()
  expect(requests.mutationReasons).toEqual(["新增轮值成员", "新增轮值成员"])
  expect(requests.reauthRequests()).toBe(1)

  await page.getByRole("button", {name: "停用"}).click()
  const destructiveDialog = page.getByRole("dialog", {name: "停用用户"})
  await expect(destructiveDialog).toBeVisible()
  await destructiveDialog.getByRole("button", {name: "取消"}).click()
  await expect(destructiveDialog).toBeHidden()
  expect(requests.mutationReasons).toHaveLength(2)
  expect(pageErrors).toEqual([])
})
