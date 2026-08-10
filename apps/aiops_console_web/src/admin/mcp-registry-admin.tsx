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
} from "@/admin/admin-client"
import { useAdminAction } from "@/admin/admin-action"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
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
type MCPIntegrationCreateInput = Omit<MCPIntegrationCreate, "reason">
type MCPIntegrationUpdateInput = Omit<MCPIntegrationUpdate, "reason">


export function MCPRegistryAdmin() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const integrations = useQuery({
    queryKey: ["mcp-integrations"], queryFn: getMCPIntegrations, retry: false,
  })
  const audit = useQuery({queryKey: ["admin-audit"], queryFn: getAdminAudit, retry: false})
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (work: () => Promise<unknown>) => {
    setPending(true)
    setError(null)
    try {
      await work()
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ["mcp-integrations"]}),
        queryClient.invalidateQueries({queryKey: ["admin-audit"]}),
      ])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "MCP Integration 操作失败")
      throw cause
    } finally {
      setPending(false)
    }
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
    pending={pending}
    error={error}
    onCreate={(body) => requestAction({title: "注册 MCP Integration", summary: `将注册 MCP Integration ${body.name} 并保存其允许范围。`, run: (reason) => run(() => createMCPIntegration({...body, reason}))})}
    onUpdate={(id, body) => requestAction({title: "保存 MCP Integration", summary: `将保存 MCP Integration ${id} 的 endpoint、能力和允许范围，并重新验证。`, run: (reason) => run(() => updateMCPIntegration(id, {...body, reason}))})}
    onToggle={(integration) => requestAction({
      title: integration.enabled ? "停用 MCP Integration" : "启用 MCP Integration",
      summary: `${integration.enabled ? "将停用" : "将启用"} MCP Integration ${integration.name}，影响新请求使用其只读能力。`,
      destructive: integration.enabled,
      run: (reason) => run(() => updateMCPIntegration(integration.id, {
        name: integration.name,
        endpoint: integration.endpoint,
        capabilities: integration.capabilities,
        allowed_scope: integration.allowed_scope,
        enabled: !integration.enabled,
        expected_revision: integration.revision,
        reason,
      })),
    })}
    onVerify={(id) => requestAction({title: "验证 MCP Integration", summary: `将验证 MCP Integration ${id} 的 capability snapshot 与健康状态。`, run: (reason) => run(() => verifyMCPIntegration(id, reason))})}
  />
}


export function MCPRegistryAdminView({
  integrations,
  audit,
  pending,
  error,
  onCreate,
  onUpdate,
  onToggle,
  onVerify,
}: {
  integrations: MCPIntegration[]
  audit: AdminAuditEntry[]
  pending: boolean
  error: string | null
  onCreate: (body: MCPIntegrationCreateInput) => void
  onUpdate: (id: string, body: MCPIntegrationUpdateInput) => void
  onToggle: (integration: MCPIntegration) => void
  onVerify: (id: string) => void
}) {
  return <div className="flex min-w-0 flex-col gap-7">
    {error ? <Alert variant="destructive"><AlertTitle>MCP Integration 操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

    <CreateIntegrationForm pending={pending} onCreate={onCreate} />

    <section className="flex flex-col gap-5 border-t pt-6" aria-labelledby="mcp-integrations-heading">
      <div>
        <h2 id="mcp-integrations-heading" className="text-base font-semibold">已注册 Integration</h2>
        <p className="mt-1 text-sm text-muted-foreground">只有启用、验证通过且 capability snapshot 未变化的只读能力可供新请求使用。</p>
      </div>
      {integrations.map((integration) => <IntegrationEditor
        key={integration.id}
        integration={integration}
        pending={pending}
        onUpdate={onUpdate}
        onToggle={onToggle}
        onVerify={onVerify}
      />)}
      {integrations.length === 0 ? <div className="border-y py-10 text-center text-sm text-muted-foreground">暂无 MCP Integration</div> : null}
    </section>

    <AuditHistory audit={audit} />
  </div>
}


function CreateIntegrationForm({
  pending,
  onCreate,
}: {
  pending: boolean
  onCreate: (body: MCPIntegrationCreateInput) => void
}) {
  const [scopes, setScopes] = useState<AllowedScope[]>([{cluster_id: "", namespace: null}])
  return <section aria-labelledby="register-mcp-heading"><Accordion><AccordionItem value="register-mcp">
    <AccordionTrigger><span><span id="register-mcp-heading" className="block font-medium">注册 MCP Integration</span><span className="mt-1 block text-xs font-normal text-muted-foreground">填写 endpoint、只读能力和允许范围</span></span></AccordionTrigger>
    <AccordionContent><form className="flex flex-col gap-4" onSubmit={(event) => {
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
      <div><Button type="submit" disabled={pending}><PlusIcon />注册</Button></div>
    </form></AccordionContent>
  </AccordionItem></Accordion>
  </section>
}


function IntegrationEditor({
  integration,
  pending,
  onUpdate,
  onToggle,
  onVerify,
}: {
  integration: MCPIntegration
  pending: boolean
  onUpdate: (id: string, body: MCPIntegrationUpdateInput) => void
  onToggle: (integration: MCPIntegration) => void
  onVerify: (id: string) => void
}) {
  const [scopes, setScopes] = useState(integration.allowed_scope)
  const inputId = `mcp-edit-${integration.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`
  const blocked = pending
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
        <Button type="button" size="sm" variant={integration.enabled ? "destructive" : "outline"} disabled={blocked} onClick={() => onToggle(integration)}>{integration.enabled ? "停用" : "启用"}</Button>
      </div>
    </div>

    <div className="grid gap-4 text-sm lg:grid-cols-3">
      <div><div className="text-xs font-medium text-muted-foreground">允许范围</div><div className="mt-1 flex flex-col gap-1">{integration.allowed_scope.map(scopeLabel).map((scope) => <span key={scope}>{scope}</span>)}</div></div>
      <Snapshot label="已验证 snapshot" capabilities={integration.verified_capability_snapshot} />
      <Snapshot label="当前 snapshot" capabilities={integration.capability_snapshot} />
    </div>

    <Accordion><AccordionItem value={`edit-${integration.id}`}>
      <AccordionTrigger><span><span className="block font-medium">编辑配置</span><span className="mt-1 block text-xs font-normal text-muted-foreground">更新 endpoint、credential、能力和允许范围</span></span></AccordionTrigger>
      <AccordionContent><form className="flex flex-col gap-4" onSubmit={(event) => {
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
      </form></AccordionContent>
    </AccordionItem></Accordion>
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
