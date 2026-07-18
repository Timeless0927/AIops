import fs from "node:fs/promises"
import { createRequire } from "node:module"

const require = createRequire(`${process.cwd()}/package.json`)
const { chromium } = require("playwright")

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"))
const base = new URL(input.base_url)
const browser = await chromium.launch({headless: true, args: ["--no-proxy-server"]})
const context = await browser.newContext({
  viewport: {width: 1440, height: 900},
  storageState: {cookies: [], origins: []},
  serviceWorkers: "block",
})
const page = await context.newPage()
const origins = new Set()
const paths = new Set()
const actions = []
const pendingMutations = new Map()
const resultTasks = []
let authenticatedEventStreamStatus = null
let authenticatedEventStreamContentType = null

const callback = async (kind, payload) => {
  const response = await fetch(`${input.mutation_callback.url}/${kind}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${input.mutation_callback.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(`Console mutation ${kind} callback returned HTTP ${response.status}`)
}

if (input.mutation_callback) {
  await page.route("**/*", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin === base.origin && url.pathname === "/api/v1/admin/users" && request.method() === "POST") {
      const requestId = (await request.allHeaders())["x-request-id"]
      if (!requestId) throw new Error("Console user creation lacks X-Request-ID")
      await callback("intent", {request_id: requestId, method: request.method(), path: url.pathname})
      pendingMutations.set(request, requestId)
    }
    await route.continue()
  })
  page.on("response", (response) => {
    const requestId = pendingMutations.get(response.request())
    if (!requestId) return
    resultTasks.push((async () => {
      const payload = await response.json().catch(() => ({}))
      if (payload.request_id !== requestId) throw new Error("Console user creation request identity mismatch")
      const identities = {}
      if (typeof payload.user?.id === "string") identities["user.id"] = payload.user.id
      if (Number.isInteger(payload.user?.revision)) identities["user.revision"] = payload.user.revision
      if (typeof payload.user?.updated_at === "string" || Number.isFinite(payload.user?.updated_at)) {
        identities["user.updated_at"] = String(payload.user.updated_at)
      }
      await callback("result", {
        request_id: requestId,
        status: response.status(),
        response_request_id: payload.request_id,
        identities,
      })
    })())
  })
}

page.on("request", (request) => {
  const url = new URL(request.url())
  if (url.protocol === "http:" || url.protocol === "https:") {
    origins.add(url.origin)
    paths.add(url.pathname)
  }
})

const responseFor = (method, path) => page.waitForResponse((response) => {
  const request = response.request()
  return request.method() === method && new URL(response.url()).pathname === path
    && response.status() >= 200 && response.status() < 300
}, {timeout: 30_000})

try {
  await page.goto(input.base_url, {waitUntil: "networkidle", timeout: 30_000})
  if (input.action === "i05_user") {
    await page.getByLabel("用户名", {exact: true}).fill(input.admin_username)
    await page.getByLabel("密码", {exact: true}).fill(input.admin_password)
    await Promise.all([
      responseFor("POST", "/auth/login"),
      page.getByRole("button", {name: "登录", exact: true}).click(),
    ])
    actions.push("admin_login")
    await page.goto(new URL("/admin", base).toString(), {waitUntil: "networkidle", timeout: 30_000})
    await page.getByRole("heading", {name: "平台管理"}).waitFor()
    await page.getByLabel("变更原因", {exact: true}).fill("I05 create ordinary acceptance User")
    await page.getByLabel("重新认证", {exact: true}).fill(input.admin_password)
    await Promise.all([
      responseFor("POST", "/auth/reauth"),
      page.getByRole("button", {name: "验证", exact: true}).click(),
    ])
    actions.push("admin_fresh_auth")
    const exists = await page.evaluate(async (username) => {
      const response = await fetch("/api/v1/admin/users", {headers: {Accept: "application/json"}})
      if (!response.ok) throw new Error(`GET /api/v1/admin/users returned HTTP ${response.status}`)
      return (await response.json()).users.some((item) => item.username === username)
    }, input.user_username)
    if (exists) throw new Error("ordinary acceptance User already exists before I05")
    await page.getByRole("tab", {name: "用户", exact: true}).click()
    const panel = page.getByRole("tabpanel")
    await panel.getByLabel("用户名", {exact: true}).fill(input.user_username)
    await panel.getByLabel("显示名称", {exact: true}).fill("I05 Ordinary User")
    await panel.getByLabel("初始密码", {exact: true}).fill(input.user_password)
    await Promise.all([
      responseFor("POST", "/api/v1/admin/users"),
      panel.getByRole("button", {name: "创建用户", exact: true}).click(),
    ])
    actions.push("create_ordinary_user")
    await page.evaluate(async () => fetch("/api/v1/actor", {headers: {Accept: "application/json"}}))
  } else {
    await page.evaluate(async () => {
      await fetch("/api/v1/actor", {headers: {Accept: "application/json"}})
      await fetch("/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username: "acceptance-invalid", password: "invalid", session_mode: "cookie"}),
      })
      await fetch("/api/v1/platform/status/stream", {
        headers: {Accept: "text/event-stream"},
      })
    })
  }
  if (input.action !== "i05_user" && input.username && input.password) {
    const status = await page.evaluate(async ({username, password}) => {
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username, password, session_mode: "cookie"}),
      })
      return response.status
    }, {username: input.username, password: input.password})
    if (status !== 200) throw new Error(`browser login returned HTTP ${status}`)
    const eventStream = await page.evaluate(async () => {
      const response = await fetch("/api/v1/platform/status/stream", {
        headers: {Accept: "text/event-stream"},
      })
      await response.body?.cancel()
      return {status: response.status, contentType: response.headers.get("content-type")}
    })
    authenticatedEventStreamStatus = eventStream.status
    authenticatedEventStreamContentType = eventStream.contentType
    await page.goto(base.origin, {waitUntil: "networkidle", timeout: 30_000})
    const platformLink = page.locator('a[href="/platform"]').first()
    await platformLink.waitFor({state: "visible", timeout: 15_000})
    await platformLink.click()
    await page.waitForURL(new URL("/platform", base).toString(), {timeout: 15_000})
  }
  await fs.mkdir(input.screenshot_dir, {recursive: true})
  const screenshot = (path) => page.screenshot({
    path,
    fullPage: true,
    mask: [page.locator('input[type="password"], [data-sensitive="true"]')],
    maskColor: "#000000",
  })
  await screenshot(`${input.screenshot_dir}/desktop.png`)
  await page.setViewportSize({width: 390, height: 844})
  await page.reload({waitUntil: "networkidle", timeout: 30_000})
  await screenshot(`${input.screenshot_dir}/mobile.png`)
  const mobileNoOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
  )
  await Promise.all(resultTasks)
  const origin = base.origin
  process.stdout.write(JSON.stringify({
    base_url: origin,
    same_origin: [...origins].every((item) => item === origin),
    origins: [...origins].sort(),
    paths: [...paths].sort(),
    actions,
    browser_context: {
      role: input.action === "i05_user" ? "platform_administrator" : input.username ? input.role : "anonymous",
      persistent: false,
      storage_state_loaded: false,
    },
    screenshots_masked: true,
    desktop_nav_reentry: Boolean(input.action === "i05_user" || input.username && input.password),
    mobile_no_overflow: mobileNoOverflow,
    authenticated_event_stream_status: authenticatedEventStreamStatus,
    authenticated_event_stream_content_type: authenticatedEventStreamContentType,
  }))
} finally {
  await browser.close()
}
