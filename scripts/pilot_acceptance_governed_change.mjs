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
    headers: {Authorization: `Bearer ${input.mutation_callback.token}`, "Content-Type": "application/json"},
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(`mutation callback returned HTTP ${response.status}`)
}

const identities = (value, prefix = "", depth = 0, result = {}) => {
  if (!value || typeof value !== "object" || depth > 2 || Object.keys(result).length >= 32) return result
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (key !== "request_id" && (key === "id" || key.endsWith("_id") || key === "revision"
        || key === "sequence"
        || key.endsWith("_revision")) && (typeof item === "string" || Number.isInteger(item))) {
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

const denialProbe = async (method, path, body = undefined) => page.evaluate(
  async ({method, path, body}) => {
    const requestId = crypto.randomUUID()
    const headers = {Accept: "application/json", "X-Request-ID": requestId}
    if (method !== "GET") {
      const csrfResponse = await fetch("/auth/csrf", {
        credentials: "same-origin", headers: {Accept: "application/json"},
      })
      if (!csrfResponse.ok) throw new Error(`CSRF read returned HTTP ${csrfResponse.status}`)
      const csrf = await csrfResponse.json()
      headers["Content-Type"] = "application/json"
      headers["X-CSRF-Token"] = csrf.csrf_token
    }
    const response = await fetch(path, {
      method, credentials: "same-origin", headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = await response.json()
    return {
      method, path, status: response.status,
      request_id: requestId,
      response_request_id: payload.request_id,
      error_code: payload.error?.code,
      payload_keys: Object.keys(payload).sort(),
      error_keys: Object.keys(payload.error ?? {}).sort(),
    }
  },
  {method, path, body},
)

const readJson = async (path) => page.evaluate(async (path) => {
  const response = await fetch(path, {credentials: "same-origin", headers: {Accept: "application/json"}})
  return {status: response.status, payload: await response.json()}
}, path)

try {
  await page.goto(base.origin, {waitUntil: "networkidle", timeout: 30_000})
  await page.getByLabel("用户名", {exact: true}).fill(input.username)
  await page.getByLabel("密码", {exact: true}).fill(input.password)
  await Promise.all([
    responseFor("POST", "/auth/login"),
    page.getByRole("button", {name: "登录", exact: true}).click(),
  ])
  const incidentPath = `/incidents/${input.incident_id}`
  if (!["r03_admin", "v08_destination_receipt"].includes(input.action)) {
    await page.goto(new URL(incidentPath, base).toString(), {waitUntil: "networkidle", timeout: 30_000})
  }
  let result = {}
  if (input.action === "v08_destination_receipt") {
    await page.goto(new URL("/admin?section=notifications", base).toString(), {waitUntil: "networkidle", timeout: 30_000})
    await page.getByLabel("重新认证", {exact: true}).fill(input.password)
    await Promise.all([
      responseFor("POST", "/auth/reauth"),
      page.getByRole("button", {name: "验证", exact: true}).click(),
    ])
    await page.getByLabel("变更原因", {exact: true}).fill(input.reason)
    const row = page.getByRole("row").filter({hasText: input.destination_name})
    const testPath = `/api/v1/admin/notification-destinations/${input.destination_id}/test`
    const tested = await Promise.all([
      responseFor("POST", testPath),
      row.getByRole("button", {name: "测试", exact: true}).click(),
    ]).then(([value]) => value.json())
    result = {verification: tested.verification}
  } else if (input.action === "v08_reinvestigate") {
    const reinvestigatePath = `/api/v1/incidents/${input.incident_id}/reinvestigate`
    const reinvestigated = await Promise.all([
      responseFor("POST", reinvestigatePath),
      page.getByRole("button", {name: "重新调查", exact: true}).click(),
    ]).then(([value]) => value.json())
    result = {investigation: reinvestigated.investigation}
  } else if (["v04", "v08_create", "r03_prepare", "r06_prepare"].includes(input.action)) {
    await page.getByLabel("Desired outcome", {exact: true}).fill(input.desired_outcome)
    await page.getByLabel("Context", {exact: true}).fill(input.context)
    const createPath = `/api/v1/incidents/${input.incident_id}/change-requests`
    const response = await Promise.all([
      responseFor("POST", createPath),
      page.getByRole("button", {name: "创建变更请求", exact: true}).click(),
    ]).then(([value]) => value)
    const created = await response.json()
    if (created.change_request?.status === "expired" && input.action === "r06_prepare") {
      throw new Error("R06 prepared Change became expired")
    }
    if (created.change_request?.status === "expired") {
      const retryPath = `/api/v1/change-requests/${created.change_request.id}/retry`
      await Promise.all([
        responseFor("POST", retryPath),
        page.getByRole("button", {name: "重试规划", exact: true}).click(),
      ])
    }
    if (["v04", "v08_create"].includes(input.action)) {
      result = {change_request: created.change_request}
    } else {
      const recoveryGate = input.action === "r06_prepare" ? "R06" : "R03"
      let detail
      let review
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const current = await readJson(`/api/v1/change-requests/${created.change_request.id}`)
        detail = current.payload.change_request
        if (current.status === 200 && detail?.status === "awaiting_approval") {
          const approval = await readJson(`/api/v1/change-requests/${created.change_request.id}/phase-approval`)
          review = approval.payload.phase_review
          if (approval.status === 200 && review) break
        }
        if (!["planning", "validating"].includes(detail?.status)) {
          throw new Error(`${recoveryGate} prepared Change became ${detail?.status ?? "unknown"}`)
        }
        await new Promise((resolve) => setTimeout(resolve, 1000))
      }
      const change = review?.changes?.[0]
      if (!review || !change) throw new Error(`${recoveryGate} prepared Change did not reach awaiting approval`)
      const canonical = change.canonical_change ?? {}
      const target = canonical.target ?? {}
      result = {prepared: {
        incident_id: input.incident_id,
        change_request_id: created.change_request.id,
        phase_id: review.phase_id,
        revision_id: review.revision_id,
        dry_run_hash: change.dry_run_hash,
        target_confirmation: change.target_confirmation,
        approval_status: "awaiting_approval",
        target_identity: {
          uid: target.uid,
          resource_version: target.resource_version,
        },
        change_summary: {
          target: {
            api_version: target.api_version,
            kind: target.kind,
            namespace: target.namespace,
            name: target.name,
          },
          operation: canonical.operation,
          diff: change.diff,
          post_checks: change.post_checks,
          rollback: change.rollback,
        },
      }}
    }
  } else if (["r05", "v08_denial"].includes(input.action)) {
    const reviewPath = `/api/v1/change-requests/${input.change_request_id}/phase-approval`
    const approvalPath = `${reviewPath}/approve`
    const executionPath = `/api/v1/change-requests/${input.change_request_id}/phase-execution/start`
    const denials = [
      await denialProbe("GET", reviewPath),
      await denialProbe("POST", approvalPath, {
        revision_id: input.revision_id,
        dry_run_hashes: [input.dry_run_hash],
        target_confirmations: [input.target_confirmation],
        rollback_policy: "stop_only",
        reason: `Verify no-Authority Approval denial for run ${input.run_id}`,
        idempotency_key: `r05-denied-approval:${input.run_id}`,
      }),
      await denialProbe("POST", executionPath, {
        phase_id: input.phase_id,
        reason: `Verify no-Authority Grant denial for run ${input.run_id}`,
        idempotency_key: `r05-denied-execution:${input.run_id}`,
        execution_timeout_seconds: 300,
      }),
    ]
    result = {
      phase_review_visible: await page.getByText(/(?:API Server dry-run|Frozen approval) diff/).count() > 0,
      approval_control_visible: await page.getByRole("button", {name: "审批 Phase", exact: true}).count() > 0,
      execution_control_visible: await page.getByRole("button", {name: "执行 Change", exact: true}).count() > 0,
      denials,
    }
  } else if (["v05", "v08_execute", "r06_approve", "r06_start"].includes(input.action)) {
    await page.getByLabel("重新认证", {exact: true}).fill(input.password)
    await Promise.all([
      responseFor("POST", "/auth/reauth"),
      page.getByRole("button", {name: "验证", exact: true}).click(),
    ])
    let approved
    if (input.action !== "r06_start") {
      await page.getByLabel("精确目标确认", {exact: true}).fill(input.target_confirmation)
      await page.getByLabel("审批原因", {exact: true}).fill(input.approval_reason)
      if (input.action === "r06_approve") {
        await page.getByLabel("回滚策略", {exact: true}).click()
        await page.getByRole("option", {name: "仅停止后续步骤", exact: true}).click()
      }
      const approvePath = `/api/v1/change-requests/${input.change_request_id}/phase-approval/approve`
      approved = await Promise.all([
        responseFor("POST", approvePath),
        page.getByRole("button", {name: "审批 Phase", exact: true}).click(),
      ]).then(([value]) => value.json())
    }
    if (input.action === "r06_approve") {
      result = {phase_review: approved.phase_review}
    } else {
      await page.getByLabel("执行原因", {exact: true}).fill(input.execution_reason)
      await page.getByLabel("Timeout (seconds)", {exact: true}).fill("300")
      const startPath = `/api/v1/change-requests/${input.change_request_id}/phase-execution/start`
      const started = await Promise.all([
        responseFor("POST", startPath),
        page.getByRole("button", {name: "执行 Change", exact: true}).click(),
      ]).then(([value]) => value.json())
      result = {
        ...(approved ? {phase_review: approved.phase_review} : {}),
        phase_execution: started.phase_execution,
      }
    }
  } else if (input.action === "r03_admin") {
    const liveEvidence = await denialProbe("POST", "/api/v1/admin/connector-commands", {
      cluster_id: input.cluster_id,
      namespace: "aiops-verification",
      action: "get_resource",
      parameters: {resource_kind: "pods", output: "json"},
      reason: `R03 verify Connector unavailable ${input.operation_id}`,
    })
    if (liveEvidence.status !== 409 || liveEvidence.error_code !== "cluster_not_ready") {
      throw new Error(`R03 live Evidence probe returned HTTP ${liveEvidence.status}`)
    }
    result = {live_evidence: liveEvidence}
  } else if (input.action === "r03_sre") {
    await page.getByLabel("Desired outcome", {exact: true}).fill(input.desired_outcome)
    await page.getByLabel("Context", {exact: true}).fill(input.context)
    const createPath = `/api/v1/incidents/${input.incident_id}/change-requests`
    const dryResponse = await Promise.all([
      responseFor("POST", createPath),
      page.getByRole("button", {name: "创建变更请求", exact: true}).click(),
    ]).then(([value]) => value)
    const dryPayload = await dryResponse.json()
    const dryRun = {
      request_id: dryResponse.request().headers()["x-request-id"],
      status: dryResponse.status(),
      response_request_id: dryPayload.request_id,
      error_code: dryPayload.error?.code,
    }
    if (dryRun.status !== 409 || dryRun.error_code !== "cluster_not_ready") {
      throw new Error(`R03 dry-run probe returned HTTP ${dryRun.status}`)
    }
    await page.getByLabel("重新认证", {exact: true}).fill(input.password)
    await Promise.all([
      responseFor("POST", "/auth/reauth"),
      page.getByRole("button", {name: "验证", exact: true}).click(),
    ])
    const root = `/api/v1/change-requests/${input.prepared.change_request_id}`
    await page.getByLabel("精确目标确认", {exact: true}).fill(input.prepared.target_confirmation)
    await page.getByLabel("审批原因", {exact: true}).fill(`R03 prepare exact Grant probe ${input.operation_id}`)
    const approvalResponse = await Promise.all([
      responseFor("POST", `${root}/phase-approval/approve`),
      page.getByRole("button", {name: "审批 Phase", exact: true}).click(),
    ]).then(([value]) => value)
    const approvalPayload = await approvalResponse.json()
    const approval = {
      request_id: approvalResponse.request().headers()["x-request-id"],
      status: approvalResponse.status(),
      response_request_id: approvalPayload.request_id,
      error_code: approvalPayload.error?.code,
    }
    if (approval.status !== 201) throw new Error(`R03 probe Approval returned HTTP ${approval.status}`)
    await page.getByLabel("执行原因", {exact: true}).fill(`R03 verify Grant fail-closed ${input.operation_id}`)
    await page.getByLabel("Timeout (seconds)", {exact: true}).fill("300")
    const grantResponse = await Promise.all([
      responseFor("POST", `${root}/phase-execution/start`),
      page.getByRole("button", {name: "执行 Change", exact: true}).click(),
    ]).then(([value]) => value)
    const grantPayload = await grantResponse.json()
    const grant = {
      request_id: grantResponse.request().headers()["x-request-id"],
      status: grantResponse.status(),
      response_request_id: grantPayload.request_id,
      error_code: grantPayload.error?.code,
    }
    if (grant.status !== 409 || grant.error_code !== "cluster_not_ready") {
      throw new Error(`R03 Grant probe returned HTTP ${grant.status}`)
    }
    const projection = await readJson(`${root}/phase-execution`)
    result = {
      denials: {dry_run: dryRun, grant},
      approval,
      active_commands: projection.payload.phase_execution?.command_id ? 1 : 0,
      grants_created: projection.payload.phase_execution?.grant?.id ? 1 : 0,
    }
  } else {
    throw new Error("unsupported governed change browser action")
  }
  await Promise.all(resultTasks)
  await page.locator('input[type="password"]').evaluateAll((inputs) => {
    for (const input of inputs) input.value = ""
  })
  await fs.mkdir(input.screenshot_dir, {recursive: true})
  await page.screenshot({path: `${input.screenshot_dir}/${input.action}.png`, fullPage: true})
  process.stdout.write(JSON.stringify({
    action: input.action,
    same_origin: [...origins].every((origin) => origin === base.origin),
    screenshots_masked: true,
    origins: [...origins].sort(),
    paths: [...paths].sort(),
    ...result,
  }))
} finally {
  await context.close()
  await browser.close()
}
