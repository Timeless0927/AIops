import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FlaskConicalIcon, KeyRoundIcon, PlusIcon, RouteIcon, XIcon } from "lucide-react"

import {
  createNotificationDestination,
  createNotificationRoute,
  getNotificationDeliveries,
  getNotificationDestinations,
  getNotificationRoutes,
  getNotificationTemplates,
  selectNotificationPilotRoute,
  simulateNotificationRoute,
  testNotificationDestination,
  updateNotificationDestination,
  updateNotificationRoute,
  type NotificationDestination,
  type NotificationSimulation,
} from "@/api/client"
import { ApiError, newClientId } from "@/api/transport"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { NotificationTemplateAdmin } from "./notification-template-admin"
import { NotificationNoiseAdmin } from "./notification-noise-admin"

export const notificationEvents = [
  "incident.opened", "incident.severity_changed", "incident.reopened", "incident.resolved",
  "investigation.needs_input", "investigation.partial", "investigation.failed",
  "change.awaiting_approval", "change.approved", "change.succeeded", "change.failed",
  "change.outcome_unknown", "change.rollback_started", "change.rolled_back",
  "change.rollback_failed", "change.effect_observed", "change.reconciliation_accepted",
  "connector.offline", "connector.recovered",
]
export const maxNotificationRoutePriority = 2_147_483_645
type CredentialMutation = {requestId: string; run: (requestId: string) => Promise<unknown>}

export function NotificationAdmin({reason}: {reason: string}) {
  const queryClient = useQueryClient()
  const destinations = useQuery({
    queryKey: ["notification-destinations"], queryFn: getNotificationDestinations, retry: false,
    refetchInterval: (query) => query.state.data?.destinations.some((item) => item.verification.state === "verifying") ? 1000 : false,
  })
  const verificationDeliveries = useQuery({
    queryKey: ["notification-deliveries", "verification"],
    queryFn: getNotificationDeliveries,
    enabled: destinations.data?.destinations.some((item) => item.verification.state === "verifying") ?? false,
    refetchInterval: destinations.data?.destinations.some((item) => item.verification.state === "verifying") ? 1000 : false,
  })
  const routes = useQuery({queryKey: ["notification-routes"], queryFn: getNotificationRoutes, retry: false})
  const templates = useQuery({queryKey: ["notification-templates"], queryFn: getNotificationTemplates, retry: false})
  const [provider, setProvider] = useState<"feishu" | "dingtalk" | "smtp">("feishu")
  const [routeAction, setRouteAction] = useState<"fanout" | "suppress">("fanout")
  const [selectedDestinations, setSelectedDestinations] = useState<string[]>([])
  const [selectedTemplate, setSelectedTemplate] = useState("")
  const [simulation, setSimulation] = useState<NotificationSimulation | null>(null)
  const [repairingDestination, setRepairingDestination] = useState<NotificationDestination | null>(null)
  const [unknownCredential, setUnknownCredential] = useState<CredentialMutation | null>(null)
  const refresh = () => {
    queryClient.invalidateQueries({queryKey: ["notification-destinations"]})
    queryClient.invalidateQueries({queryKey: ["notification-routes"]})
    queryClient.invalidateQueries({queryKey: ["notification-templates"]})
    queryClient.invalidateQueries({queryKey: ["notification-deliveries"]})
  }
  const mutation = useMutation({mutationFn: (work: () => Promise<unknown>) => work(), onSuccess: refresh})
  const credentialMutation = useMutation({
    mutationFn: (operation: CredentialMutation) => operation.run(operation.requestId),
    onSuccess: () => {
      setUnknownCredential(null)
      setRepairingDestination(null)
      refresh()
    },
    onError: (error, operation) => {
      if (
        error instanceof ApiError
        && ["outcome_unknown", "outcome_reconciliation_required"].includes(error.code)
        && error.requestId
      ) {
        setUnknownCredential({...operation, requestId: error.requestId})
      } else {
        setUnknownCredential(null)
      }
    },
  })
  const submitCredential = (run: CredentialMutation["run"]) => credentialMutation.mutate({
    requestId: newClientId(), run,
  })
  const simulate = useMutation({mutationFn: simulateNotificationRoute, onSuccess: (result) => setSimulation(result.simulation)})

  if (destinations.isPending || routes.isPending || templates.isPending) return <div className="py-10 text-center text-sm text-muted-foreground" role="status">正在加载通知配置</div>
  if (destinations.isError || routes.isError || templates.isError) return <div className="py-10 text-center text-sm text-destructive">无法读取通知配置</div>
  const destinationItems = destinations.data.destinations
  const retryAtByOperation = Object.fromEntries(
    (verificationDeliveries.data?.deliveries ?? [])
      .filter((delivery) => delivery.is_test && delivery.next_attempt_at !== null)
      .map((delivery) => [delivery.id, delivery.next_attempt_at]),
  )

  return <div className="flex flex-col gap-8">
    {mutation.isError || simulate.isError || (credentialMutation.isError && !unknownCredential) ? <Alert variant="destructive"><AlertTitle>通知配置操作失败</AlertTitle><AlertDescription>{String((mutation.error || simulate.error || credentialMutation.error) instanceof Error ? (mutation.error || simulate.error || credentialMutation.error)?.message : "请求失败")}</AlertDescription></Alert> : null}
    {unknownCredential ? <Alert><AlertTitle>凭据变更结果待确认</AlertTitle><AlertDescription className="flex flex-wrap items-center gap-3"><span>对账完成前已阻止新的凭据变更。</span><Button type="button" size="sm" variant="outline" disabled={credentialMutation.isPending} onClick={() => credentialMutation.mutate(unknownCredential)}>使用同一 request ID 对账</Button></AlertDescription></Alert> : null}
    <section className="flex flex-col gap-4" aria-labelledby="destination-heading">
      <h2 id="destination-heading" className="text-lg font-semibold">Notification Destination</h2>
      <form className="flex flex-col gap-3" onSubmit={(event) => {
        event.preventDefault()
        const form = new FormData(event.currentTarget)
        const common = {name: String(form.get("name") || ""), provider, reason}
        const config = notificationConfigFromForm(provider, form)
        submitCredential((requestId) => createNotificationDestination(
          {...common, config} as Parameters<typeof createNotificationDestination>[0], requestId,
        ))
      }}>
        <FieldGroup className="grid gap-3 md:grid-cols-[1fr_180px_2fr_auto]">
          <Field><FieldLabel htmlFor="notification-destination-name">名称</FieldLabel><Input id="notification-destination-name" name="name" required /></Field>
          <Field><FieldLabel htmlFor="notification-provider">Provider</FieldLabel><Select value={provider} onValueChange={(value) => setProvider(value as typeof provider)}><SelectTrigger id="notification-provider" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["feishu", "dingtalk", "smtp"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
          <NotificationConfigFields provider={provider} idPrefix="notification-create" />
          <div className="flex items-end"><Button type="submit" disabled={!reason || credentialMutation.isPending || Boolean(unknownCredential)}><PlusIcon />创建</Button></div>
        </FieldGroup>
      </form>
      <NotificationDestinationTable
        destinations={destinationItems}
        reason={reason}
        pending={mutation.isPending || credentialMutation.isPending || Boolean(unknownCredential)}
        retryAtByOperation={retryAtByOperation}
        onTest={(destination) => mutation.mutate(() => testNotificationDestination(destination.id, destination.configuration_revision, reason))}
        onSelect={(destination) => mutation.mutate(() => selectNotificationPilotRoute(destination.id, destination.configuration_revision, reason))}
        onRepair={setRepairingDestination}
        onDisable={(destination) => mutation.mutate(() => updateNotificationDestination(destination.id, {enabled: false, expected_revision: destination.configuration_revision, reason}))}
      />
      {repairingDestination ? <NotificationDestinationRepairForm
        destination={repairingDestination}
        pending={credentialMutation.isPending || Boolean(unknownCredential)}
        onCancel={() => setRepairingDestination(null)}
        onSubmit={(config) => submitCredential(
          (requestId) => updateNotificationDestination(repairingDestination.id, {
            config,
            expected_revision: repairingDestination.configuration_revision,
            reason,
          }, requestId),
        )}
      /> : null}
    </section>

    <NotificationTemplateAdmin reason={reason} />
    <NotificationNoiseAdmin reason={reason} />

    <section className="flex flex-col gap-4 border-t pt-6" aria-labelledby="route-heading">
      <h2 id="route-heading" className="text-lg font-semibold">Notification Route</h2>
      <form className="grid gap-3 lg:grid-cols-[1fr_120px_160px_2fr_220px_auto]" onSubmit={(event) => {
        event.preventDefault(); const form = new FormData(event.currentTarget)
        const match = Object.fromEntries(["event", "severity", "environment", "team", "service"].map((key) => [key, split(form.get(key))]).filter(([, values]) => (values as string[]).length))
        mutation.mutate(() => createNotificationRoute({name: String(form.get("name") || ""), priority: Number(form.get("priority")), enabled: false, match, ...(routeAction === "fanout" ? {destination_ids: selectedDestinations, ...(selectedTemplate ? {template_id: selectedTemplate} : {})} : {suppress_reason: String(form.get("suppress_reason") || "")}), reason}))
      }}>
        <Field><FieldLabel htmlFor="route-name">名称</FieldLabel><Input id="route-name" name="name" required /></Field><Field><FieldLabel htmlFor="route-priority">Priority</FieldLabel><Input id="route-priority" name="priority" type="number" min="0" max={maxNotificationRoutePriority} required /></Field><Field><FieldLabel htmlFor="route-action">动作</FieldLabel><Select value={routeAction} onValueChange={(value) => setRouteAction(value as typeof routeAction)}><SelectTrigger id="route-action" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="fanout">Fan-out</SelectItem><SelectItem value="suppress">Suppress</SelectItem></SelectGroup></SelectContent></Select></Field>
        {routeAction === "fanout" ? <fieldset className="flex flex-wrap items-end gap-3"><legend className="mb-2 text-sm font-medium">Destinations</legend>{destinationItems.filter((item) => item.enabled).map((item) => <label key={item.id} className="flex items-center gap-2 text-sm"><Checkbox checked={selectedDestinations.includes(item.id)} onCheckedChange={(checked) => setSelectedDestinations(checked ? [...selectedDestinations, item.id] : selectedDestinations.filter((id) => id !== item.id))} />{item.name}</label>)}</fieldset> : <Field><FieldLabel htmlFor="suppress-reason">Suppress reason</FieldLabel><Input id="suppress-reason" name="suppress_reason" required /></Field>}
        <Field><FieldLabel htmlFor="route-template">Template</FieldLabel><Select value={selectedTemplate || "builtin"} onValueChange={(value) => setSelectedTemplate(value === "builtin" ? "" : value ?? "")} disabled={routeAction === "suppress"}><SelectTrigger id="route-template" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="builtin">Built-in</SelectItem>{templates.data.templates.filter((item) => !item.is_builtin && item.enabled).map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <div className="flex items-end"><Button type="submit" disabled={!reason || mutation.isPending || (routeAction === "fanout" && !selectedDestinations.length)}><PlusIcon />创建</Button></div>
        <Field className="lg:col-span-6"><FieldLabel htmlFor="route-event">Exact match（逗号分隔，留空表示任意）</FieldLabel><div className="grid gap-2 md:grid-cols-5"><Input id="route-event" name="event" placeholder="event" /><Input name="severity" placeholder="severity" aria-label="Severity match" /><Input name="environment" placeholder="Environment" aria-label="Environment match" /><Input name="team" placeholder="Team ID" aria-label="Team match" /><Input name="service" placeholder="Service ID" aria-label="Service match" /></div></Field>
      </form>
      <Table><TableHeader><TableRow><TableHead>Priority</TableHead><TableHead>Route</TableHead><TableHead>Match</TableHead><TableHead>结果</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{routes.data.routes.map((route) => <TableRow key={route.id}><TableCell>{route.priority}</TableCell><TableCell className="font-medium">{route.name}{route.is_default ? <Badge className="ml-2" variant="outline">default</Badge> : null}</TableCell><TableCell className="text-xs text-muted-foreground">{Object.entries(route.match).map(([key, value]) => `${key}=${value.join("|")}`).join(" · ") || "全部"}</TableCell><TableCell>{route.suppress_reason ? `Suppress: ${route.suppress_reason}` : `${route.destination_ids.length} destinations · ${route.template_id ? "custom" : "built-in"}`}</TableCell><TableCell>{route.is_default ? <Badge variant="secondary">启用</Badge> : <Button type="button" size="sm" variant="outline" disabled={!reason || mutation.isPending} onClick={() => mutation.mutate(() => updateNotificationRoute(route.id, {enabled: !route.enabled, reason}))}>{route.enabled ? "停用" : "启用"}</Button>}</TableCell></TableRow>)}</TableBody></Table>
    </section>

    <section className="flex flex-col gap-4 border-t pt-6" aria-labelledby="simulation-heading"><div><h2 id="simulation-heading" className="text-lg font-semibold">Route simulation</h2></div><form className="grid gap-3 md:grid-cols-6" onSubmit={(event) => {event.preventDefault(); const form = new FormData(event.currentTarget); const eventType = String(form.get("event") || "incident.opened"); simulate.mutate({event_id: `simulation:${newClientId()}`, event_type: eventType, occurred_at: Date.now() / 1000, severity: String(form.get("severity") || "warning"), subject: {type: notificationSubjectType(eventType), id: "simulation", version: 1}, scope: compact({environment: String(form.get("environment") || ""), team_id: String(form.get("team") || ""), service_id: String(form.get("service") || "")}), summary: "Route simulation", facts: notificationSimulationFacts(eventType), console_path: "/admin"})}}><Field><FieldLabel htmlFor="simulation-event">Event</FieldLabel><Select name="event" defaultValue="incident.opened"><SelectTrigger id="simulation-event" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{notificationEvents.map((event) => <SelectItem key={event} value={event}>{event}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="simulation-severity">Severity</FieldLabel><Select name="severity" defaultValue="warning"><SelectTrigger id="simulation-severity" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["info", "warning", "error", "critical"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="simulation-environment">Environment</FieldLabel><Input id="simulation-environment" name="environment" defaultValue="prod" required /></Field><Field><FieldLabel htmlFor="simulation-team">Team</FieldLabel><Input id="simulation-team" name="team" /></Field><Field><FieldLabel htmlFor="simulation-service">Service</FieldLabel><Input id="simulation-service" name="service" /></Field><div className="flex items-end"><Button type="submit" variant="outline" disabled={simulate.isPending}><FlaskConicalIcon />模拟</Button></div></form>{simulation ? <Alert><FlaskConicalIcon /><AlertTitle>{simulation.route_name}</AlertTitle><AlertDescription>{simulation.suppressed_reason ?? simulation.destinations.map((item) => item.name).join(", ")}</AlertDescription></Alert> : null}</section>
  </div>
}

export function NotificationDestinationTable({
  destinations, reason, pending, retryAtByOperation, onTest, onSelect, onRepair, onDisable,
}: {
  destinations: NotificationDestination[]
  reason: string
  pending: boolean
  retryAtByOperation: Record<string, number | null>
  onTest: (destination: NotificationDestination) => void
  onSelect: (destination: NotificationDestination) => void
  onRepair: (destination: NotificationDestination) => void
  onDisable: (destination: NotificationDestination) => void
}) {
  return <Table>
    <TableHeader><TableRow><TableHead>名称</TableHead><TableHead>Provider</TableHead><TableHead>配置状态</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
    <TableBody>{destinations.map((destination) => {
      const operationId = destination.verification.operation_id
      const retryAt = operationId ? retryAtByOperation[operationId] : null
      return <TableRow key={destination.id}>
        <TableCell className="font-medium">{destination.name}</TableCell>
        <TableCell>{destination.provider}</TableCell>
        <TableCell className="max-w-sm text-xs text-muted-foreground">{maskedSummary(destination)}</TableCell>
        <TableCell><div className="flex flex-col items-start gap-1"><div className="flex flex-wrap gap-2"><Badge variant={destinationStatusVariant(destination)}>{destinationStatusLabel(destination)}</Badge>{destination.availability.state === "degraded" ? <Badge variant="outline">可用性降级</Badge> : null}</div>{retryAt !== null && retryAt !== undefined ? <span className="text-xs text-muted-foreground">下次重试 {formatRetryAt(retryAt)}</span> : null}</div></TableCell>
        <TableCell><div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={!reason || pending || destination.verification.state === "verifying"} onClick={() => onTest(destination)}><FlaskConicalIcon />测试</Button>
          <Button type="button" size="sm" variant="outline" disabled={!reason || pending} onClick={() => onRepair(destination)}><KeyRoundIcon />修复凭据</Button>
          {destination.verification.state === "verified" && destination.availability.state === "available" && !destination.pilot_route_selected ? <Button type="button" size="sm" variant="outline" disabled={!reason || pending} onClick={() => onSelect(destination)}><RouteIcon />设为 Pilot Route</Button> : null}
          {destination.enabled && !destination.pilot_route_selected ? <Button type="button" size="sm" variant="outline" disabled={!reason || pending} onClick={() => onDisable(destination)}>停用</Button> : null}
        </div></TableCell>
      </TableRow>
    })}</TableBody>
  </Table>
}

export function NotificationDestinationRepairForm({
  destination, pending, onSubmit, onCancel,
}: {
  destination: NotificationDestination
  pending: boolean
  onSubmit: (config: ReturnType<typeof notificationConfigFromForm>) => void
  onCancel: () => void
}) {
  const headingId = `notification-repair-${destination.id}`
  return <form className="flex flex-col gap-3 border-t pt-4" aria-labelledby={headingId} onSubmit={(event) => {
    event.preventDefault()
    onSubmit(notificationConfigFromForm(destination.provider, new FormData(event.currentTarget)))
  }}>
    <div className="flex items-center justify-between gap-3"><h3 id={headingId} className="text-sm font-semibold">修复 {destination.name} 凭据</h3><Button type="button" size="icon-sm" variant="ghost" onClick={onCancel} aria-label="取消修复"><XIcon /></Button></div>
    <NotificationConfigFields provider={destination.provider} idPrefix={`notification-repair-${destination.id}`} />
    <div><Button type="submit" disabled={pending}><KeyRoundIcon />保存新 revision</Button></div>
  </form>
}

function NotificationConfigFields({provider, idPrefix}: {provider: NotificationDestination["provider"], idPrefix: string}) {
  if (provider === "smtp") return <div className="grid gap-3 md:grid-cols-3">
    <Field><FieldLabel htmlFor={`${idPrefix}-host`}>SMTP host</FieldLabel><Input id={`${idPrefix}-host`} name="host" required /></Field><Field><FieldLabel htmlFor={`${idPrefix}-port`}>端口</FieldLabel><Input id={`${idPrefix}-port`} name="port" type="number" min="1" max="65535" defaultValue="587" required /></Field><Field><FieldLabel htmlFor={`${idPrefix}-tls`}>TLS</FieldLabel><Select name="tls_mode" defaultValue="starttls"><SelectTrigger id={`${idPrefix}-tls`} className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="starttls">STARTTLS</SelectItem><SelectItem value="ssl">TLS</SelectItem></SelectGroup></SelectContent></Select></Field>
    <Field><FieldLabel htmlFor={`${idPrefix}-user`}>用户名</FieldLabel><Input id={`${idPrefix}-user`} name="username" required /></Field><Field><FieldLabel htmlFor={`${idPrefix}-password`}>密码</FieldLabel><Input id={`${idPrefix}-password`} name="password" type="password" autoComplete="new-password" required /></Field><Field><FieldLabel htmlFor={`${idPrefix}-from`}>发件地址</FieldLabel><Input id={`${idPrefix}-from`} name="from_address" type="email" required /></Field><Field className="md:col-span-3"><FieldLabel htmlFor={`${idPrefix}-to`}>收件地址</FieldLabel><Input id={`${idPrefix}-to`} name="to_addresses" type="text" placeholder="ops@example.com, sre@example.com" required /></Field>
  </div>
  return <div className="grid gap-3 md:grid-cols-2"><Field><FieldLabel htmlFor={`${idPrefix}-webhook`}>Webhook URL</FieldLabel><Input id={`${idPrefix}-webhook`} name="webhook_url" type="url" required /></Field>{provider === "dingtalk" ? <Field><FieldLabel htmlFor={`${idPrefix}-signing-secret`}>Signing secret</FieldLabel><Input id={`${idPrefix}-signing-secret`} name="signing_secret" type="password" autoComplete="new-password" required /></Field> : null}</div>
}

function split(value: FormDataEntryValue | null) { return String(value || "").split(",").map((item) => item.trim()).filter(Boolean) }
function notificationConfigFromForm(provider: NotificationDestination["provider"], form: FormData) { return provider === "smtp" ? {host: String(form.get("host") || ""), port: Number(form.get("port")), username: String(form.get("username") || ""), password: String(form.get("password") || ""), from_address: String(form.get("from_address") || ""), to_addresses: split(form.get("to_addresses")), tls_mode: String(form.get("tls_mode") || "starttls") as "starttls" | "ssl"} : provider === "dingtalk" ? {webhook_url: String(form.get("webhook_url") || ""), signing_secret: String(form.get("signing_secret") || "")} : {webhook_url: String(form.get("webhook_url") || "")} }
function formatRetryAt(value: number) { return new Intl.DateTimeFormat("zh-CN", {dateStyle: "short", timeStyle: "medium"}).format(new Date(value * 1000)) }
function maskedSummary(destination: NotificationDestination) { return Object.entries(destination.config).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`).join(" · ") }
function destinationStatusLabel(destination: NotificationDestination) { if (destination.pilot_route_selected && destination.readiness === "ready") return "Pilot Ready"; return {verified: "已验证", verifying: "验证中", failed: "验证失败", stale: "配置已变更", unverified: "未验证", not_applicable: "未配置"}[destination.verification.state] }
function destinationStatusVariant(destination: NotificationDestination): "positive" | "outline" | "destructive" | "secondary" { if (destination.pilot_route_selected && destination.readiness === "ready") return "positive"; if (destination.verification.state === "failed" || destination.verification.state === "stale") return "destructive"; if (destination.verification.state === "verified" || destination.verification.state === "verifying") return "outline"; return "secondary" }
function compact(value: Record<string, string>) { return Object.fromEntries(Object.entries(value).filter(([, item]) => item)) }
export function notificationSubjectType(event: string) { return event.startsWith("change.") ? "change_request" : event.split(".")[0] }
export function notificationSimulationFacts(event: string) { const status = event.split(".")[1]; if (event === "incident.severity_changed") return {incident_id: "simulation", status, previous_severity: "low", severity: "high"}; if (event.startsWith("incident.")) return {incident_id: "simulation", status}; if (event.startsWith("investigation.")) return {incident_id: "simulation", investigation_id: "simulation", status, reason: "simulation"}; if (event === "change.awaiting_approval") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", status}; if (event === "change.approved") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", approval_id: "simulation", status}; if (event === "change.effect_observed" || event === "change.reconciliation_accepted") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", reconciliation_id: "simulation", status}; if (event.startsWith("change.")) return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", execution_id: "simulation", status}; return {connector_id: "simulation", cluster_id: "simulation", status} }
