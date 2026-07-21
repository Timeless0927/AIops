import fs from "node:fs/promises"
import {createRequire} from "node:module"

const require = createRequire(`${process.cwd()}/package.json`)
const {chromium} = require("playwright")
const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"))
const base = new URL(input.base_url)
const browser = await chromium.launch({headless: true, args: ["--no-proxy-server"]})
const context = await browser.newContext({
  viewport: {width: 1440, height: 1000},
  storageState: {cookies: [], origins: []},
  serviceWorkers: "block",
})
const page = await context.newPage()
const origins = new Set()
const paths = new Set()
const pending = new Map()
const resultTasks = []

const callback = async (kind, payload) => {
  if (!input.mutation_callback) throw new Error("Console mutation callback is required")
  const response = await fetch(`${input.mutation_callback.url}/${kind}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${input.mutation_callback.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(`mutation callback returned HTTP ${response.status}`)
}

const identities = (value, prefix = "", depth = 0, result = {}) => {
  if (!value || typeof value !== "object" || depth > 2 || Object.keys(result).length >= 32) return result
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (key !== "request_id" && (key === "id" || key.endsWith("_id") || key === "revision"
        || key.endsWith("_revision") || key === "version")
        && (typeof item === "string" || Number.isInteger(item))) {
      result[path] = item
    } else if (item && typeof item === "object") identities(item, path, depth + 1, result)
  }
  return result
}

await page.route("**/*", async (route) => {
  const request = route.request()
  const url = new URL(request.url())
  if (url.origin === base.origin && url.pathname.startsWith("/api/v1/")
      && ["POST", "PATCH", "PUT", "DELETE"].includes(request.method())) {
    const requestId = (await request.allHeaders())["x-request-id"]
    if (!requestId) throw new Error(`Console mutation ${request.method()} ${url.pathname} lacks X-Request-ID`)
    await callback("intent", {request_id: requestId, method: request.method(), path: url.pathname})
    pending.set(request, requestId)
  }
  await route.continue()
})

page.on("response", (response) => {
  const requestId = pending.get(response.request())
  if (!requestId) return
  resultTasks.push((async () => {
    const payload = await response.json().catch(() => ({}))
    await callback("result", {
      request_id: requestId,
      status: response.status(),
      response_request_id: payload.request_id,
      identities: identities(payload),
      error_code: payload.error?.code,
    })
  })())
})

page.on("request", (request) => {
  const url = new URL(request.url())
  if (["http:", "https:"].includes(url.protocol)) {
    origins.add(url.origin)
    paths.add(url.pathname)
  }
})

const responseFor = (method, path) => page.waitForResponse((response) => {
  const request = response.request()
  return request.method() === method && new URL(response.url()).pathname === path
}, {timeout: 30_000})

try {
  await page.goto(base.origin, {waitUntil: "networkidle", timeout: 30_000})
  await page.getByLabel("用户名", {exact: true}).fill(input.username)
  await page.getByLabel("密码", {exact: true}).fill(input.password)
  await Promise.all([
    responseFor("POST", "/auth/login"),
    page.getByRole("button", {name: "登录", exact: true}).click(),
  ])
  const reportPagePath = `/incidents/${input.incident_id}/report`
  const reportApiPath = `/api/v1/incidents/${input.incident_id}/report`
  await page.goto(new URL(reportPagePath, base).toString(), {
    waitUntil: "networkidle", timeout: 30_000,
  })
  await page.getByRole("heading", {name: "事件报告", exact: true}).waitFor()
  for (const [field, label] of Object.entries({
    impact: "影响范围",
    root_cause: "根因说明",
    resolution_summary: "解决摘要",
    follow_up: "后续事项",
  })) {
    await page.getByLabel(label, {exact: true}).fill(input.narrative[field])
  }
  page.once("dialog", (dialog) => dialog.accept())
  const [updatedResponse, publishedResponse] = await Promise.all([
    responseFor("PATCH", reportApiPath),
    responseFor("POST", `${reportApiPath}/publish`),
    page.getByRole("button", {name: "发布版本", exact: true}).click(),
  ])
  if (!updatedResponse.ok() || publishedResponse.status() !== 201) {
    throw new Error("Console Report publication did not return the exact success statuses")
  }
  const published = await publishedResponse.json()
  await Promise.all(resultTasks)
  await page.locator('input[type="password"]').evaluateAll((inputs) => {
    for (const input of inputs) input.value = ""
  })
  await fs.mkdir(input.screenshot_dir, {recursive: true})
  await page.screenshot({path: `${input.screenshot_dir}/v07.png`, fullPage: true})
  process.stdout.write(JSON.stringify({
    action: "v07",
    same_origin: [...origins].every((origin) => origin === base.origin),
    screenshots_masked: true,
    origins: [...origins].sort(),
    paths: [...paths].sort(),
    publication: published.publication,
  }))
} finally {
  await context.close()
  await browser.close()
}
