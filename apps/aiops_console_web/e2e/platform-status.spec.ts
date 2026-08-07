import { expect, test, type Page, type Route } from "@playwright/test"


const status = {
  request_id: "platform-status:e2e",
  generated_at: 1_700_000_010,
  raw_error: "must-not-leak-owner-secret",
  capabilities: {
    model: {
      readiness: "not_ready", configuration: "present", configuration_revision: "model:1",
      setup_decision: "active",
      verification: {operation_id: null, state: "failed", revision: "model:1", checked_at: 1_700_000_000, reason_code: "owner_unavailable"},
      availability: {state: "unavailable", observed_at: 1_700_000_001, reason_code: "owner_unavailable"},
    },
    notification: {
      readiness: "not_ready", configuration: "absent", configuration_revision: null,
      setup_decision: "active",
      verification: {operation_id: null, state: "not_applicable", revision: null, checked_at: null, reason_code: "not_configured"},
      availability: {state: "unavailable", observed_at: null, reason_code: "not_configured"},
    },
    connector: {
      readiness: "ready", configuration: "present", configuration_revision: null,
      setup_decision: "active",
      verification: {operation_id: null, state: "verified", revision: null, checked_at: 1_700_000_002, reason_code: null},
      availability: {state: "degraded", observed_at: 1_700_000_003, reason_code: "connector_degraded"},
      connection: {states: ["offline", "online"], total: 2, online: 1},
    },
    observability: {
      readiness: "ready", configuration: "present", configuration_revision: null,
      setup_decision: "active",
      verification: {operation_id: null, state: "verified", revision: null, checked_at: 1_700_000_004, reason_code: null},
      availability: {state: "available", observed_at: 1_700_000_004, reason_code: null},
    },
  },
}

async function json(route: Route, body: object, statusCode = 200) {
  await route.fulfill({status: statusCode, contentType: "application/json", body: JSON.stringify(body)})
}

async function mockGateway(page: Page, isAdministrator: boolean) {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === "/api/v1/actor") {
      return json(route, {
        request_id: "actor:e2e",
        actor: {
          id: "user:e2e", username: "operator", display_name: "E2E Operator",
          roles: isAdministrator ? ["platform_administrator"] : ["sre"],
          capabilities: [], is_platform_administrator: isAdministrator,
        },
      })
    }
    if (url.pathname === "/api/v1/platform/status") return json(route, status)
    if (url.pathname === "/api/v1/incidents") {
      return json(route, {request_id: "incidents:e2e", incidents: []})
    }
    if (url.pathname === "/auth/csrf") {
      return json(route, {request_id: "csrf:e2e", csrf_token: "test-only-csrf"})
    }
    if (url.pathname === "/api/v1/admin/model-provider/test") {
      return json(route, {
        request_id: "model-test:e2e",
        error: {code: "fresh_auth_required", message: "需要重新验证密码后再执行"},
      }, 403)
    }
    await route.continue()
  })
}

test("platform rail survives desktop/mobile re-entry and bounds partial owner failures", async ({page}, testInfo) => {
  await mockGateway(page, true)
  await page.goto("/platform")

  await expect(page.getByRole("heading", {name: "平台状态"})).toBeVisible()
  await expect(page.getByText("能力 owner 暂不可用")).toBeVisible()
  await expect(page.getByText("must-not-leak-owner-secret")).toHaveCount(0)
  await expect(page.getByText("test-only-csrf")).toHaveCount(0)

  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    page: document.documentElement.scrollWidth,
    railClient: document.querySelector<HTMLElement>('nav[aria-label="平台能力"]')!.clientWidth,
    railScroll: document.querySelector<HTMLElement>('nav[aria-label="平台能力"]')!.scrollWidth,
  }))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  if (testInfo.project.name.startsWith("mobile")) {
    expect(dimensions.railScroll).toBeGreaterThan(dimensions.railClient)
  }

  await page.getByRole("link", {name: /进入事件工作区/}).click()
  await expect(page).toHaveURL(/\/incidents$/)
  if (testInfo.project.name.startsWith("mobile")) {
    await page.getByRole("button", {name: "切换侧栏"}).click()
    await page.getByRole("link", {name: "平台状态"}).click()
  } else {
    await page.getByRole("link", {name: "平台状态"}).click()
  }
  await expect(page).toHaveURL(/\/platform$/)
  await expect(page.getByRole("heading", {name: "平台状态"})).toBeVisible()

  await page.getByLabel("操作原因").fill("验证恢复后的模型连接")
  await page.getByRole("button", {name: "验证 / 重试"}).click()
  await expect(page.getByText("需要重新验证密码后再执行")).toBeVisible()
})

test("ordinary users receive a secret-free read-only platform summary", async ({page}) => {
  await mockGateway(page, false)
  await page.goto("/platform")

  await expect(page.getByText("只读安全摘要").first()).toBeVisible()
  await expect(page.getByRole("button", {name: "验证 / 重试"})).toHaveCount(0)
  await expect(page.getByRole("button", {name: "暂时跳过"})).toHaveCount(0)
  await expect(page.getByRole("link", {name: "管理详细配置"})).toHaveCount(0)
  await expect(page.locator("body")).not.toContainText("must-not-leak-owner-secret")
})
