import fs from "node:fs/promises"
import {createRequire} from "node:module"

const require = createRequire(`${process.cwd()}/package.json`)
const {chromium} = require("playwright")
const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"))
const base = new URL(input.base_url)
const browser = await chromium.launch({headless: true, args: ["--no-proxy-server"]})
const context = await browser.newContext({viewport: {width: 1440, height: 1000}})
const page = await context.newPage()
const origins = new Set()
const paths = new Set()
const actions = []

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

const read = async (path) => page.evaluate(async (target) => {
  const response = await fetch(target, {headers: {Accept: "application/json"}})
  if (!response.ok) throw new Error(`GET ${target} returned HTTP ${response.status}`)
  return response.json()
}, path)

const poll = async (readValue, predicate, timeout = 120_000) => {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const value = await readValue()
    if (predicate(value)) return value
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  throw new Error("Console state did not converge before the acceptance deadline")
}

const tab = async (name) => {
  await page.getByRole("tab", {name, exact: true}).click()
  return page.getByRole("tabpanel")
}

const choose = async (panel, label, option) => {
  await panel.getByLabel(label, {exact: true}).click()
  await page.getByRole("option", {name: option, exact: true}).click()
}

try {
  await page.goto(base.origin, {waitUntil: "networkidle", timeout: 30_000})
  await page.getByLabel("用户名", {exact: true}).fill(input.admin_username)
  await page.getByLabel("密码", {exact: true}).fill(input.admin_password)
  await Promise.all([
    responseFor("POST", "/auth/login"),
    page.getByRole("button", {name: "登录", exact: true}).click(),
  ])
  actions.push("admin_login")
  await page.goto(new URL("/admin", base).toString(), {waitUntil: "networkidle", timeout: 30_000})
  await page.getByRole("heading", {name: "平台管理"}).waitFor()
  await page.getByLabel("变更原因", {exact: true}).fill("A02 controlled verification setup")
  await page.getByLabel("重新认证", {exact: true}).fill(input.admin_password)
  await Promise.all([
    responseFor("POST", "/auth/reauth"),
    page.getByRole("button", {name: "验证", exact: true}).click(),
  ])
  actions.push("admin_fresh_auth")

  let admin = await read("/api/v1/admin/users")
  let user = admin.users.find((item) => item.username === input.sre_username)
  if (!user) {
    const panel = await tab("用户")
    await panel.getByLabel("用户名", {exact: true}).fill(input.sre_username)
    await panel.getByLabel("显示名称", {exact: true}).fill("A02 Verification SRE")
    await panel.getByLabel("初始密码", {exact: true}).fill(input.sre_password)
    await Promise.all([
      responseFor("POST", "/api/v1/admin/users"),
      panel.getByRole("button", {name: "创建用户", exact: true}).click(),
    ])
    actions.push("create_sre_user")
    admin = await poll(() => read("/api/v1/admin/users"),
      (value) => value.users.some((item) => item.username === input.sre_username))
    user = admin.users.find((item) => item.username === input.sre_username)
  }

  const teamName = "A02 Pilot Verification"
  let team = admin.teams.find((item) => item.name === teamName)
  if (!team) {
    const panel = await tab("团队")
    await panel.getByLabel("团队名称", {exact: true}).fill(teamName)
    await panel.getByLabel("说明", {exact: true}).fill("Controlled verification owner")
    await Promise.all([
      responseFor("POST", "/api/v1/admin/teams"),
      panel.getByRole("button", {name: "创建团队", exact: true}).click(),
    ])
    actions.push("create_team")
    admin = await poll(() => read("/api/v1/admin/users"),
      (value) => value.teams.some((item) => item.name === teamName))
    team = admin.teams.find((item) => item.name === teamName)
  }

  if (!admin.team_memberships.some((item) => item.active && item.user_id === user.id && item.team_id === team.id)) {
    const panel = await tab("成员关系")
    await choose(panel, "用户", "A02 Verification SRE")
    await choose(panel, "团队", teamName)
    await Promise.all([
      responseFor("POST", "/api/v1/admin/team-memberships"),
      panel.getByRole("button", {name: "添加成员", exact: true}).click(),
    ])
    actions.push("create_team_membership")
  }
  admin = await read("/api/v1/admin/users")
  if (!admin.role_bindings.some((item) => item.active && item.user_id === user.id
      && item.role === "sre" && item.scope_type === "team" && item.scope_id === team.id)) {
    const panel = await tab("角色绑定")
    await choose(panel, "用户", "A02 Verification SRE")
    await choose(panel, "角色", "SRE")
    await choose(panel, "团队范围", teamName)
    await Promise.all([
      responseFor("POST", "/api/v1/admin/role-bindings"),
      panel.getByRole("button", {name: "添加绑定", exact: true}).click(),
    ])
    actions.push("create_sre_role_binding")
  }

  let connectors = await read("/api/v1/admin/connector-enrollments")
  let cluster = connectors.clusters.find((item) => item.cluster_id === "pilot-cluster")
  if (!cluster) throw new Error("pilot-cluster is not registered")
  if (cluster.environment !== "test" || cluster.mutation_enabled !== true) {
    const panel = await tab("Cluster")
    const form = panel.locator("form:has(#cluster-name-pilot-cluster)")
    await form.getByLabel("Environment", {exact: true}).click()
    await page.getByRole("option", {name: "test", exact: true}).click()
    const mutation = form.getByRole("checkbox", {name: "允许 mutation", exact: true})
    if (await mutation.getAttribute("data-state") !== "checked") await mutation.click()
    await form.getByLabel("治理备注", {exact: true}).fill("A02 isolated non-production fixture")
    await Promise.all([
      responseFor("PATCH", "/api/v1/admin/clusters/pilot-cluster"),
      form.getByRole("button", {name: "保存", exact: true}).click(),
    ])
    actions.push("configure_non_production_cluster")
    connectors = await poll(() => read("/api/v1/admin/connector-enrollments"),
      (value) => value.clusters.some((item) => item.cluster_id === "pilot-cluster"
        && item.environment === "test" && item.mutation_enabled === true))
    cluster = connectors.clusters.find((item) => item.cluster_id === "pilot-cluster")
  }

  let candidate = await poll(
    () => read("/api/v1/admin/resource-catalog"),
    (value) => value.discovery_candidates.some((item) => item.cluster_id === "pilot-cluster"
      && item.namespace === "aiops-verification" && item.workload_kind === "Deployment"
      && item.workload_name === "verification-api" && item.deleted_at === null),
  ).then((value) => value.discovery_candidates.find((item) => item.cluster_id === "pilot-cluster"
    && item.namespace === "aiops-verification" && item.workload_kind === "Deployment"
    && item.workload_name === "verification-api" && item.deleted_at === null))

  let catalog = await read("/api/v1/admin/resource-catalog")
  let service = catalog.services.find((item) => item.name === "verification-api" && item.team_id === team.id)
  if (!service) {
    const panel = await tab("资源目录")
    await choose(panel, "责任团队", teamName)
    await panel.getByLabel("Service 名称", {exact: true}).fill("verification-api")
    await panel.getByLabel("说明", {exact: true}).fill("A02 controlled verification workload")
    await Promise.all([
      responseFor("POST", "/api/v1/admin/services"),
      panel.getByRole("button", {name: "创建 Service", exact: true}).click(),
    ])
    actions.push("create_service")
    catalog = await poll(() => read("/api/v1/admin/resource-catalog"),
      (value) => value.services.some((item) => item.name === "verification-api" && item.team_id === team.id))
    service = catalog.services.find((item) => item.name === "verification-api" && item.team_id === team.id)
  }

  let binding = candidate.resource_binding_id
    ? catalog.resource_bindings.find((item) => item.id === candidate.resource_binding_id)
    : null
  if (!binding || binding.service_id !== service.id) {
    await page.reload({waitUntil: "networkidle", timeout: 30_000})
    await page.getByLabel("变更原因", {exact: true}).fill("A02 controlled verification setup")
    const panel = await tab("资源目录")
    await choose(panel, "确认或纠正为", `verification-api · ${teamName}`)
    const row = panel.getByRole("row").filter({hasText: "Deployment/verification-api"})
      .filter({hasText: "aiops-verification"})
    const [bindingResponse] = await Promise.all([
      page.waitForResponse((response) => {
        const request = response.request()
        const path = new URL(response.url()).pathname
        return ["POST", "PATCH"].includes(request.method())
          && path.startsWith("/api/v1/admin/resource-bindings")
      }, {timeout: 30_000}),
      row.getByRole("button", {name: /\u786e\u8ba4|\u7ea0\u6b63/}).click(),
    ])
    if (!bindingResponse.ok()) {
      const payload = await bindingResponse.json()
      throw new Error(`Resource Binding returned HTTP ${bindingResponse.status()} (${payload.error?.code ?? "request_failed"})`)
    }
    actions.push("confirm_resource_binding")
    catalog = await poll(() => read("/api/v1/admin/resource-catalog"),
      (value) => value.discovery_candidates.some((item) => item.id === candidate.id
        && item.resource_binding_id))
    const boundCandidate = catalog.discovery_candidates.find((item) => item.id === candidate.id)
    candidate = boundCandidate
    binding = catalog.resource_bindings.find((item) => item.id === boundCandidate.resource_binding_id)
  }

  let authorities = await read("/api/v1/admin/kubernetes-change-authorities")
  let authority = authorities.kubernetes_change_authorities.find((item) => item.active
    && item.user_id === user.id && item.environment === "test" && item.scope_type === "namespace"
    && item.scope.cluster_id === "pilot-cluster" && item.scope.namespace === "aiops-verification")
  if (!authority) {
    const panel = await tab("Kubernetes 变更权限")
    await choose(panel, "用户", "A02 Verification SRE")
    await choose(panel, "Environment", "test")
    await choose(panel, "Authority scope", "Namespace")
    await choose(panel, "Cluster", "pilot-cluster")
    await panel.getByLabel("Namespace", {exact: true}).fill("aiops-verification")
    await Promise.all([
      responseFor("POST", "/api/v1/admin/kubernetes-change-authorities"),
      panel.getByRole("button", {name: "授予变更权限", exact: true}).click(),
    ])
    actions.push("create_namespace_authority")
    authorities = await poll(() => read("/api/v1/admin/kubernetes-change-authorities"),
      (value) => value.kubernetes_change_authorities.some((item) => item.active
        && item.user_id === user.id && item.environment === "test" && item.scope_type === "namespace"
        && item.scope.cluster_id === "pilot-cluster" && item.scope.namespace === "aiops-verification"))
    authority = authorities.kubernetes_change_authorities.find((item) => item.active
      && item.user_id === user.id && item.environment === "test" && item.scope_type === "namespace"
      && item.scope.cluster_id === "pilot-cluster" && item.scope.namespace === "aiops-verification")
  }

  await fs.mkdir(input.screenshot_dir, {recursive: true})
  await page.screenshot({path: `${input.screenshot_dir}/v01-console.png`, fullPage: true})
  process.stdout.write(JSON.stringify({
    same_origin: [...origins].every((item) => item === base.origin),
    origins: [...origins].sort(),
    paths: [...paths].sort(),
    actions,
    cluster: {
      cluster_id: cluster.cluster_id,
      environment: cluster.environment,
      mutation_enabled: cluster.mutation_enabled,
    },
    sre: {id: user.id, username: user.username},
    team: {id: team.id, name: team.name},
    service: {id: service.id, name: service.name},
    binding: {
      id: binding.id,
      namespace: candidate.namespace,
      workload_kind: candidate.workload_kind,
      workload_name: candidate.workload_name,
      deployment_target_id: candidate.deployment_target_id,
      revision: binding.revision,
    },
    authority: {
      id: authority.id,
      environment: authority.environment,
      scope_type: authority.scope_type,
      scope: authority.scope,
    },
  }))
} finally {
  await browser.close()
}
