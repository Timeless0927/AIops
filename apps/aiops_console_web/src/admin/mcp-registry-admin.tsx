import { useId, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2Icon, FlaskConicalIcon, PlusIcon, SaveIcon, Trash2Icon } from "lucide-react"

import {
  createMCPIntegration,
  getAdminAudit,
  getMCPIntegrations,
  type AdminAuditEntry,
  type MCPIntegration,
  type MCPIntegrationCreate,
  type MCPIntegrationUpdate,
  updateMCPIntegration,
  verifyMCPIntegration,
} from "@/api/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"


const APPROVED_CAPABILITIES = [
  {name: "query_metrics", version: "prometheus-query-v1", label: "Prometheus metrics"},
  {name: "query_logs", version: "loki-query-v1", label: "Loki logs"},
  {name: "run_k8s_read", version: "gateway-k8s-read-v1", label: "Kubernetes read"},
  {name: "get_service_topology", version: "topology-query-v1", label: "Service topology"},
] as const

type AllowedScope = MCPIntegration["allowed_scope"][number]
type CapabilityPolicy = MCPIntegration["capabilities"][number]


export function MCPRegistryAdmin({reason}: {reason: string}) {
  const queryClient = useQueryClient()
  const integrations = useQuery({
    queryKey: ["mcp-integrations"], queryFn: getMCPIntegrations, retry: false,
  })
  const audit = useQuery({queryKey: ["admin-audit"], queryFn: getAdminAudit, retry: false})
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = (work: Promise<unknown>) => {
    setPending(true)
    setError(null)
    void work
      .then(() => Promise.all([
        queryClient.invalidateQueries({queryKey: ["mcp-integrations"]}),
        queryClient.invalidateQueries({queryKey: ["admin-audit"]}),
      ]))
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "MCP Integration 操作失败"))
      .finally(() => setPending(false))
  }

  if (integrations.isPending || audit.isPending) {
    return <div className="py-10 text-center text-sm text-muted-foreground" role="status">正在加载 MCP Integration</div>
  }
  if (integrations.isError || audit.isError) {
    return <div className="py-10 text-center text-sm text-destructive">无法读取 MCP Integration</div>
  }

  return <MCPRegistryAdminView
    integrations={integrations.data}
    audit={audit.data.filter((entry) => entry.target_type === "mcp_integration")}
    reason={reason}
    pending={pending}
    error={error}
    onCreate={(body) => run(createMCPIntegration(body))}
    onUpdate={(id, body) => run(updateMCPIntegration(id, body))}
    onVerify={(id) => run(verifyMCPIntegration(id, reason))}
  />
}


export function MCPRegistryAdminView({
  integrations,
  audit,
  reason,
  pending,
  error,
  onCreate,
  onUpdate,
  onVerify,
}: {
  integrations: MCPIntegration[]
  audit: AdminAuditEntry[]
  reason: string
  pending: boolean
  error: string | null
  onCreate: (body: MCPIntegrationCreate) => void
  onUpdate: (id: string, body: MCPIntegrationUpdate) => void
  onVerify: (id: string) => void
}) {
  return <div className="flex min-w-0 flex-col gap-7">
    {error ? <Alert variant="destructive"><AlertTitle>MCP Integration 操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

    <CreateIntegrationForm reason={reason} pending={pending} onCreate={onCreate} />

    <section className="flex flex-col gap-5 border-t pt-6" aria-labelledby="mcp-integrations-heading">
      <div>
        <h2 id="mcp-integrations-heading" className="text-base font-semibold">已注册 Integration</h2>
        <p className="mt-1 text-sm text-muted-foreground">只有启用、验证通过且 capability snapshot 未变化的只读能力可供新请求使用。</p>
      </div>
      {integrations.map((integration) => <IntegrationEditor
        key={integration.id}
        integration={integration}
        reason={reason}
        pending={pending}
        onUpdate={onUpdate}
        onVerify={onVerify}
      />)}
      {integrations.length === 0 ? <div className="border-y py-10 text-center text-sm text-muted-foreground">暂无 MCP Integration</div> : null}
    </section>

    <AuditHistory audit={audit} />
  </div>
}


function CreateIntegrationForm({
  reason,
  pending,
  onCreate,
}: {
  reason: string
  pending: boolean
  onCreate: (body: MCPIntegrationCreate) => void
}) {
  const [scopes, setScopes] = useState<AllowedScope[]>([{cluster_id: "", namespace: null}])
  return <section aria-labelledby="register-mcp-heading">
    <h2 id="register-mcp-heading" className="text-base font-semibold">注册 MCP Integration</h2>
    <form className="mt-4 flex flex-col gap-4" onSubmit={(event) => {
      event.preventDefault()
      const form = new FormData(event.currentTarget)
      const capabilities = capabilitiesFrom(form)
      if (capabilities.length === 0) return
      onCreate({
        name: String(form.get("name") || ""),
        endpoint: String(form.get("endpoint") || ""),
        ...credentialFrom(form),
        capabilities,
        allowed_scope: scopes,
        enabled: false,
        reason,
      })
      event.currentTarget.reset()
    }}>
      <div className="grid gap-3 md:grid-cols-2">
        <Field><FieldLabel htmlFor="mcp-name">名称</FieldLabel><Input id="mcp-name" name="name" required /></Field>
        <Field><FieldLabel htmlFor="mcp-endpoint">Endpoint</FieldLabel><Input id="mcp-endpoint" name="endpoint" type="url" required /></Field>
        <Field className="md:col-span-2"><FieldLabel htmlFor="mcp-credential">Credential</FieldLabel><Input id="mcp-credential" name="credential" type="password" autoComplete="new-password" /></Field>
      </div>
      <CapabilityFields selected={[]} />
      <ScopeFields scopes={scopes} onChange={setScopes} />
      <div><Button type="submit" disabled={!reason || pending}><PlusIcon />注册</Button></div>
    </form>
  </section>
}


function IntegrationEditor({
  integration,
  reason,
  pending,
  onUpdate,
  onVerify,
}: {
  integration: MCPIntegration
  reason: string
  pending: boolean
  onUpdate: (id: string, body: MCPIntegrationUpdate) => void
  onVerify: (id: string) => void
}) {
  const [scopes, setScopes] = useState(integration.allowed_scope)
  const inputId = `mcp-edit-${integration.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`
  const blocked = !reason || pending
  const toggle = () => onUpdate(integration.id, {
    name: integration.name,
    endpoint: integration.endpoint,
    capabilities: integration.capabilities,
    allowed_scope: integration.allowed_scope,
    enabled: !integration.enabled,
    expected_revision: integration.revision,
    reason,
  })

  return <article className="flex min-w-0 flex-col gap-4 border-t pt-5">
    <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-medium">{integration.name}</h3>
          <Badge variant={integration.enabled ? "positive" : "secondary"}>{integration.enabled ? "启用" : "停用"}</Badge>
          <Badge variant={integration.verification.state === "verified" ? "positive" : integration.verification.state === "failed" ? "destructive" : "secondary"}>{verificationLabel(integration.verification.state)}</Badge>
          <Badge variant={integration.health.status === "ok" ? "positive" : integration.health.status === "unavailable" ? "destructive" : "secondary"}>{healthLabel(integration.health.status)}</Badge>
          {integration.capability_changed ? <Badge variant="warning">版本变化</Badge> : null}
          <Badge variant="outline">{integration.credential_configured ? "凭据已配置" : "无凭据"}</Badge>
        </div>
        <div className="mt-2 break-all text-xs text-muted-foreground">{integration.endpoint} · {integration.revision}</div>
        {integration.verification.reason_code || integration.health.error ? <div className="mt-1 text-xs text-destructive">{integration.verification.reason_code ?? integration.health.error}</div> : null}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" disabled={blocked} onClick={() => onVerify(integration.id)}><FlaskConicalIcon />验证</Button>
        <Button type="button" size="sm" variant="outline" disabled={blocked} onClick={toggle}>{integration.enabled ? "停用" : "启用"}</Button>
      </div>
    </div>

    <div className="grid gap-4 text-sm lg:grid-cols-3">
      <div><div className="text-xs font-medium text-muted-foreground">允许范围</div><div className="mt-1 flex flex-col gap-1">{integration.allowed_scope.map(scopeLabel).map((scope) => <span key={scope}>{scope}</span>)}</div></div>
      <Snapshot label="已验证 snapshot" capabilities={integration.verified_capability_snapshot} />
      <Snapshot label="当前 snapshot" capabilities={integration.capability_snapshot} />
    </div>

    <details className="border-t pt-3">
      <summary className="cursor-pointer text-sm font-medium">编辑配置</summary>
      <form className="mt-4 flex flex-col gap-4" onSubmit={(event) => {
        event.preventDefault()
        const form = new FormData(event.currentTarget)
        const capabilities = capabilitiesFrom(form)
        if (capabilities.length === 0) return
        onUpdate(integration.id, {
          name: String(form.get("name") || ""),
          endpoint: String(form.get("endpoint") || ""),
          ...credentialFrom(form),
          capabilities,
          allowed_scope: scopes,
          enabled: integration.enabled,
          expected_revision: integration.revision,
          reason,
        })
        const credential = event.currentTarget.elements.namedItem("credential")
        if (credential instanceof HTMLInputElement) credential.value = ""
      }}>
        <div className="grid gap-3 md:grid-cols-2">
          <Field><FieldLabel htmlFor={`${inputId}-name`}>名称</FieldLabel><Input id={`${inputId}-name`} name="name" defaultValue={integration.name} required /></Field>
          <Field><FieldLabel htmlFor={`${inputId}-endpoint`}>Endpoint</FieldLabel><Input id={`${inputId}-endpoint`} name="endpoint" type="url" defaultValue={integration.endpoint} required /></Field>
          <Field className="md:col-span-2"><FieldLabel htmlFor={`${inputId}-credential`}>替换 Credential</FieldLabel><Input id={`${inputId}-credential`} name="credential" type="password" autoComplete="new-password" /></Field>
        </div>
        <CapabilityFields selected={integration.capabilities} />
        <ScopeFields scopes={scopes} onChange={setScopes} />
        <div><Button type="submit" size="sm" disabled={blocked}><SaveIcon />保存并重新验证</Button></div>
      </form>
    </details>
  </article>
}


function CapabilityFields({selected}: {selected: CapabilityPolicy[]}) {
  const id = useId()
  return <fieldset className="grid gap-2 border-y py-3 sm:grid-cols-2 lg:grid-cols-4">
    <legend className="px-1 text-sm font-medium">AIOps 只读能力</legend>
    {APPROVED_CAPABILITIES.map((capability) => <label key={capability.name} className="flex min-w-0 items-start gap-2 text-sm">
      <Checkbox name="capability" value={capability.name} defaultChecked={selected.some((item) => item.name === capability.name)} aria-labelledby={`${id}-${capability.name}`} />
      <span id={`${id}-${capability.name}`} className="min-w-0"><span className="block">{capability.label}</span><span className="block break-all text-xs text-muted-foreground">{capability.version}</span></span>
    </label>)}
  </fieldset>
}


function ScopeFields({scopes, onChange}: {scopes: AllowedScope[]; onChange: (scopes: AllowedScope[]) => void}) {
  return <fieldset className="flex flex-col gap-3">
    <legend className="text-sm font-medium">允许范围</legend>
    {scopes.map((scope, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
      <Input aria-label={`Cluster ${index + 1}`} value={scope.cluster_id} placeholder="Cluster ID" required onChange={(event) => onChange(scopes.map((item, itemIndex) => itemIndex === index ? {...item, cluster_id: event.target.value} : item))} />
      <Input aria-label={`Namespace ${index + 1}`} value={scope.namespace ?? ""} placeholder="Namespace，留空表示整个 Cluster" onChange={(event) => onChange(scopes.map((item, itemIndex) => itemIndex === index ? {...item, namespace: event.target.value || null} : item))} />
      <Button type="button" size="icon-sm" variant="ghost" aria-label={`删除范围 ${index + 1}`} disabled={scopes.length === 1} onClick={() => onChange(scopes.filter((_, itemIndex) => itemIndex !== index))}><Trash2Icon /></Button>
    </div>)}
    <div><Button type="button" size="sm" variant="outline" onClick={() => onChange([...scopes, {cluster_id: "", namespace: null}])}><PlusIcon />添加范围</Button></div>
  </fieldset>
}


function Snapshot({label, capabilities}: {label: string; capabilities: MCPIntegration["capability_snapshot"]}) {
  return <div><div className="text-xs font-medium text-muted-foreground">{label}</div>{capabilities?.length ? <ul className="mt-1 space-y-1">{capabilities.map((capability) => <li key={capability.name} className="break-all"><CheckCircle2Icon className="mr-1 inline size-3" />{capability.name} · {capability.version}</li>)}</ul> : <div className="mt-1 text-muted-foreground">暂无</div>}</div>
}


function AuditHistory({audit}: {audit: AdminAuditEntry[]}) {
  return <section className="border-t pt-6" aria-labelledby="mcp-audit-heading">
    <h2 id="mcp-audit-heading" className="text-base font-semibold">最近审计</h2>
    <div className="mt-3 divide-y border-y text-sm">
      {audit.map((entry) => <div key={entry.id} className="grid gap-1 py-3 lg:grid-cols-[1.2fr_1fr_1fr_1fr] lg:gap-4">
        <div><div className="font-medium">{entry.action}</div><div className="text-xs text-muted-foreground">{entry.target_id ?? "-"} · {entry.result}</div></div>
        <div><div>{entry.actor_id ?? "未知 actor"}</div><div className="break-all text-xs text-muted-foreground">{entry.request_id}</div></div>
        <div className="break-words text-muted-foreground">{entry.reason}</div>
        <time className="text-muted-foreground" dateTime={new Date(entry.created_at * 1000).toISOString()}>{new Date(entry.created_at * 1000).toLocaleString()}</time>
      </div>)}
      {audit.length === 0 ? <div className="py-8 text-center text-muted-foreground">暂无 MCP Integration 审计</div> : null}
    </div>
  </section>
}


function capabilitiesFrom(form: FormData): CapabilityPolicy[] {
  const selected = new Set(form.getAll("capability").map(String))
  return APPROVED_CAPABILITIES.filter((capability) => selected.has(capability.name)).map(({name, version}) => ({name, version, read_only: true}))
}


function credentialFrom(form: FormData) {
  const credential = String(form.get("credential") || "")
  return credential ? {credential} : {}
}


function scopeLabel(scope: AllowedScope) {
  return `${scope.cluster_id} / ${scope.namespace ?? "全部 namespace"}`
}


function verificationLabel(state: MCPIntegration["verification"]["state"]) {
  return ({unverified: "未验证", verified: "已验证", failed: "验证失败"})[state]
}


function healthLabel(status: MCPIntegration["health"]["status"]) {
  return ({unknown: "健康未知", ok: "健康", unavailable: "不可用"})[status]
}
