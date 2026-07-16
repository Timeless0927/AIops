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
let authenticatedEventStreamStatus = null
let authenticatedEventStreamContentType = null
page.on("request", (request) => {
  const url = new URL(request.url())
  if (url.protocol === "http:" || url.protocol === "https:") {
    origins.add(url.origin)
    paths.add(url.pathname)
  }
})

try {
  await page.goto(input.base_url, {waitUntil: "networkidle", timeout: 30_000})
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
  if (input.username && input.password) {
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
  const origin = base.origin
  process.stdout.write(JSON.stringify({
    base_url: origin,
    same_origin: [...origins].every((item) => item === origin),
    origins: [...origins].sort(),
    paths: [...paths].sort(),
    browser_context: {role: input.username ? input.role : "anonymous", persistent: false, storage_state_loaded: false},
    screenshots_masked: true,
    desktop_nav_reentry: Boolean(input.username && input.password),
    mobile_no_overflow: mobileNoOverflow,
    authenticated_event_stream_status: authenticatedEventStreamStatus,
    authenticated_event_stream_content_type: authenticatedEventStreamContentType,
  }))
} finally {
  await browser.close()
}
