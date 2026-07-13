import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FlaskConicalIcon, PlusIcon } from "lucide-react"

import {
  createNotificationDestination,
  createNotificationRoute,
  getNotificationDestinations,
  getNotificationRoutes,
  getNotificationTemplates,
  simulateNotificationRoute,
  testNotificationDestination,
  updateNotificationDestination,
  updateNotificationRoute,
  type NotificationDestination,
  type NotificationSimulation,
} from "@/api/client"
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

export function NotificationAdmin({reason}: {reason: string}) {
  const queryClient = useQueryClient()
  const destinations = useQuery({queryKey: ["notification-destinations"], queryFn: getNotificationDestinations, retry: false})
  const routes = useQuery({queryKey: ["notification-routes"], queryFn: getNotificationRoutes, retry: false})
  const templates = useQuery({queryKey: ["notification-templates"], queryFn: getNotificationTemplates, retry: false})
  const [provider, setProvider] = useState<"feishu" | "dingtalk" | "smtp">("feishu")
  const [routeAction, setRouteAction] = useState<"fanout" | "suppress">("fanout")
  const [selectedDestinations, setSelectedDestinations] = useState<string[]>([])
  const [selectedTemplate, setSelectedTemplate] = useState("")
  const [simulation, setSimulation] = useState<NotificationSimulation | null>(null)
  const refresh = () => {
    queryClient.invalidateQueries({queryKey: ["notification-destinations"]})
    queryClient.invalidateQueries({queryKey: ["notification-routes"]})
    queryClient.invalidateQueries({queryKey: ["notification-templates"]})
  }
  const mutation = useMutation({mutationFn: (work: () => Promise<unknown>) => work(), onSuccess: refresh})
  const simulate = useMutation({mutationFn: simulateNotificationRoute, onSuccess: (result) => setSimulation(result.simulation)})

  if (destinations.isPending || routes.isPending || templates.isPending) return <div className="py-10 text-center text-sm text-muted-foreground" role="status">正在加载通知配置</div>
  if (destinations.isError || routes.isError || templates.isError) return <div className="py-10 text-center text-sm text-destructive">无法读取通知配置</div>
  const destinationItems = destinations.data.destinations

  return <div className="flex flex-col gap-8">
    {mutation.isError || simulate.isError ? <Alert variant="destructive"><AlertTitle>通知配置操作失败</AlertTitle><AlertDescription>{String((mutation.error || simulate.error) instanceof Error ? (mutation.error || simulate.error)?.message : "请求失败")}</AlertDescription></Alert> : null}
    <section className="flex flex-col gap-4" aria-labelledby="destination-heading">
      <h2 id="destination-heading" className="text-lg font-semibold">Notification Destination</h2>
      <form className="flex flex-col gap-3" onSubmit={(event) => {
        event.preventDefault()
        const form = new FormData(event.currentTarget)
        const common = {name: String(form.get("name") || ""), provider, reason}
        const config = provider === "smtp" ? {
          host: String(form.get("host") || ""), port: Number(form.get("port")), username: String(form.get("username") || ""), password: String(form.get("password") || ""), from_address: String(form.get("from_address") || ""), to_addresses: split(form.get("to_addresses")), tls_mode: String(form.get("tls_mode") || "starttls") as "starttls" | "ssl",
        } : provider === "dingtalk" ? {webhook_url: String(form.get("webhook_url") || ""), signing_secret: String(form.get("signing_secret") || "")} : {webhook_url: String(form.get("webhook_url") || "")}
        mutation.mutate(() => createNotificationDestination({...common, config} as Parameters<typeof createNotificationDestination>[0]))
      }}>
        <FieldGroup className="grid gap-3 md:grid-cols-[1fr_180px_2fr_auto]">
          <Field><FieldLabel htmlFor="notification-destination-name">名称</FieldLabel><Input id="notification-destination-name" name="name" required /></Field>
          <Field><FieldLabel htmlFor="notification-provider">Provider</FieldLabel><Select value={provider} onValueChange={(value) => setProvider(value as typeof provider)}><SelectTrigger id="notification-provider" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["feishu", "dingtalk", "smtp"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
          {provider === "smtp" ? <div className="grid gap-3 md:grid-cols-3">
            <Field><FieldLabel htmlFor="smtp-host">SMTP host</FieldLabel><Input id="smtp-host" name="host" required /></Field><Field><FieldLabel htmlFor="smtp-port">端口</FieldLabel><Input id="smtp-port" name="port" type="number" min="1" max="65535" defaultValue="587" required /></Field><Field><FieldLabel htmlFor="smtp-tls">TLS</FieldLabel><Select name="tls_mode" defaultValue="starttls"><SelectTrigger id="smtp-tls" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="starttls">STARTTLS</SelectItem><SelectItem value="ssl">TLS</SelectItem></SelectGroup></SelectContent></Select></Field>
            <Field><FieldLabel htmlFor="smtp-user">用户名</FieldLabel><Input id="smtp-user" name="username" required /></Field><Field><FieldLabel htmlFor="smtp-password">密码</FieldLabel><Input id="smtp-password" name="password" type="password" autoComplete="new-password" required /></Field><Field><FieldLabel htmlFor="smtp-from">发件地址</FieldLabel><Input id="smtp-from" name="from_address" type="email" required /></Field><Field className="md:col-span-3"><FieldLabel htmlFor="smtp-to">收件地址</FieldLabel><Input id="smtp-to" name="to_addresses" type="text" placeholder="ops@example.com, sre@example.com" required /></Field>
          </div> : <div className="grid gap-3 md:grid-cols-2"><Field><FieldLabel htmlFor="notification-webhook">Webhook URL</FieldLabel><Input id="notification-webhook" name="webhook_url" type="url" required /></Field>{provider === "dingtalk" ? <Field><FieldLabel htmlFor="notification-signing-secret">Signing secret</FieldLabel><Input id="notification-signing-secret" name="signing_secret" type="password" autoComplete="new-password" required /></Field> : null}</div>}
          <div className="flex items-end"><Button type="submit" disabled={!reason || mutation.isPending}><PlusIcon />创建</Button></div>
        </FieldGroup>
      </form>
      <Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>Provider</TableHead><TableHead>配置状态</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{destinationItems.map((destination) => <TableRow key={destination.id}><TableCell className="font-medium">{destination.name}</TableCell><TableCell>{destination.provider}</TableCell><TableCell className="max-w-sm text-xs text-muted-foreground">{maskedSummary(destination)}</TableCell><TableCell><Badge variant={destination.enabled ? "positive" : destination.tested_at ? "outline" : "secondary"}>{destination.enabled ? "启用" : destination.tested_at ? "已测试" : "未测试"}</Badge></TableCell><TableCell><div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={!reason || mutation.isPending} onClick={() => mutation.mutate(() => testNotificationDestination(destination.id, reason))}><FlaskConicalIcon />测试</Button><Button type="button" size="sm" variant="outline" disabled={!reason || !destination.tested_at || mutation.isPending} onClick={() => mutation.mutate(() => updateNotificationDestination(destination.id, {enabled: !destination.enabled, reason}))}>{destination.enabled ? "停用" : "启用"}</Button></div></TableCell></TableRow>)}</TableBody></Table>
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
        <Field><FieldLabel htmlFor="route-name">名称</FieldLabel><Input id="route-name" name="name" required /></Field><Field><FieldLabel htmlFor="route-priority">Priority</FieldLabel><Input id="route-priority" name="priority" type="number" min="0" max="2147483646" required /></Field><Field><FieldLabel htmlFor="route-action">动作</FieldLabel><Select value={routeAction} onValueChange={(value) => setRouteAction(value as typeof routeAction)}><SelectTrigger id="route-action" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="fanout">Fan-out</SelectItem><SelectItem value="suppress">Suppress</SelectItem></SelectGroup></SelectContent></Select></Field>
        {routeAction === "fanout" ? <fieldset className="flex flex-wrap items-end gap-3"><legend className="mb-2 text-sm font-medium">Destinations</legend>{destinationItems.filter((item) => item.enabled).map((item) => <label key={item.id} className="flex items-center gap-2 text-sm"><Checkbox checked={selectedDestinations.includes(item.id)} onCheckedChange={(checked) => setSelectedDestinations(checked ? [...selectedDestinations, item.id] : selectedDestinations.filter((id) => id !== item.id))} />{item.name}</label>)}</fieldset> : <Field><FieldLabel htmlFor="suppress-reason">Suppress reason</FieldLabel><Input id="suppress-reason" name="suppress_reason" required /></Field>}
        <Field><FieldLabel htmlFor="route-template">Template</FieldLabel><Select value={selectedTemplate || "builtin"} onValueChange={(value) => setSelectedTemplate(value === "builtin" ? "" : value ?? "")} disabled={routeAction === "suppress"}><SelectTrigger id="route-template" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="builtin">Built-in</SelectItem>{templates.data.templates.filter((item) => !item.is_builtin && item.enabled).map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <div className="flex items-end"><Button type="submit" disabled={!reason || mutation.isPending || (routeAction === "fanout" && !selectedDestinations.length)}><PlusIcon />创建</Button></div>
        <Field className="lg:col-span-6"><FieldLabel htmlFor="route-event">Exact match（逗号分隔，留空表示任意）</FieldLabel><div className="grid gap-2 md:grid-cols-5"><Input id="route-event" name="event" placeholder="event" /><Input name="severity" placeholder="severity" aria-label="Severity match" /><Input name="environment" placeholder="Environment" aria-label="Environment match" /><Input name="team" placeholder="Team ID" aria-label="Team match" /><Input name="service" placeholder="Service ID" aria-label="Service match" /></div></Field>
      </form>
      <Table><TableHeader><TableRow><TableHead>Priority</TableHead><TableHead>Route</TableHead><TableHead>Match</TableHead><TableHead>结果</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{routes.data.routes.map((route) => <TableRow key={route.id}><TableCell>{route.priority}</TableCell><TableCell className="font-medium">{route.name}{route.is_default ? <Badge className="ml-2" variant="outline">default</Badge> : null}</TableCell><TableCell className="text-xs text-muted-foreground">{Object.entries(route.match).map(([key, value]) => `${key}=${value.join("|")}`).join(" · ") || "全部"}</TableCell><TableCell>{route.suppress_reason ? `Suppress: ${route.suppress_reason}` : `${route.destination_ids.length} destinations · ${route.template_id ? "custom" : "built-in"}`}</TableCell><TableCell>{route.is_default ? <Badge variant="secondary">启用</Badge> : <Button type="button" size="sm" variant="outline" disabled={!reason || mutation.isPending} onClick={() => mutation.mutate(() => updateNotificationRoute(route.id, {enabled: !route.enabled, reason}))}>{route.enabled ? "停用" : "启用"}</Button>}</TableCell></TableRow>)}</TableBody></Table>
    </section>

    <section className="flex flex-col gap-4 border-t pt-6" aria-labelledby="simulation-heading"><div><h2 id="simulation-heading" className="text-lg font-semibold">Route simulation</h2></div><form className="grid gap-3 md:grid-cols-6" onSubmit={(event) => {event.preventDefault(); const form = new FormData(event.currentTarget); const eventType = String(form.get("event") || "incident.opened"); simulate.mutate({event_id: `simulation:${crypto.randomUUID()}`, event_type: eventType, occurred_at: Date.now() / 1000, severity: String(form.get("severity") || "warning"), subject: {type: notificationSubjectType(eventType), id: "simulation", version: 1}, scope: compact({environment: String(form.get("environment") || ""), team_id: String(form.get("team") || ""), service_id: String(form.get("service") || "")}), summary: "Route simulation", facts: notificationSimulationFacts(eventType), console_path: "/admin"})}}><Field><FieldLabel htmlFor="simulation-event">Event</FieldLabel><Select name="event" defaultValue="incident.opened"><SelectTrigger id="simulation-event" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{notificationEvents.map((event) => <SelectItem key={event} value={event}>{event}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="simulation-severity">Severity</FieldLabel><Select name="severity" defaultValue="warning"><SelectTrigger id="simulation-severity" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["info", "warning", "error", "critical"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="simulation-environment">Environment</FieldLabel><Input id="simulation-environment" name="environment" defaultValue="prod" required /></Field><Field><FieldLabel htmlFor="simulation-team">Team</FieldLabel><Input id="simulation-team" name="team" /></Field><Field><FieldLabel htmlFor="simulation-service">Service</FieldLabel><Input id="simulation-service" name="service" /></Field><div className="flex items-end"><Button type="submit" variant="outline" disabled={simulate.isPending}><FlaskConicalIcon />模拟</Button></div></form>{simulation ? <Alert><FlaskConicalIcon /><AlertTitle>{simulation.route_name}</AlertTitle><AlertDescription>{simulation.suppressed_reason ?? simulation.destinations.map((item) => item.name).join(", ")}</AlertDescription></Alert> : null}</section>
  </div>
}

function split(value: FormDataEntryValue | null) { return String(value || "").split(",").map((item) => item.trim()).filter(Boolean) }
function maskedSummary(destination: NotificationDestination) { return Object.entries(destination.config).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`).join(" · ") }
function compact(value: Record<string, string>) { return Object.fromEntries(Object.entries(value).filter(([, item]) => item)) }
export function notificationSubjectType(event: string) { return event.startsWith("change.") ? "change_request" : event.split(".")[0] }
export function notificationSimulationFacts(event: string) { const status = event.split(".")[1]; if (event === "incident.severity_changed") return {incident_id: "simulation", status, previous_severity: "low", severity: "high"}; if (event.startsWith("incident.")) return {incident_id: "simulation", status}; if (event.startsWith("investigation.")) return {incident_id: "simulation", investigation_id: "simulation", status, reason: "simulation"}; if (event === "change.awaiting_approval") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", status}; if (event === "change.approved") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", approval_id: "simulation", status}; if (event === "change.effect_observed" || event === "change.reconciliation_accepted") return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", reconciliation_id: "simulation", status}; if (event.startsWith("change.")) return {incident_id: "simulation", change_request_id: "simulation", phase_id: "simulation", execution_id: "simulation", status}; return {connector_id: "simulation", cluster_id: "simulation", status} }
