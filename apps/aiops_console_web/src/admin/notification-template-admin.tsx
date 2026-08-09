import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CopyIcon, FlaskConicalIcon, PencilIcon } from "lucide-react"

import {
  copyNotificationTemplate, getNotificationDestinations, getNotificationTemplates,
  previewNotificationTemplate, testNotificationTemplate, updateNotificationTemplate,
  type NotificationTemplate,
  type NotificationTemplatePreview,
} from "@/admin/notification-client"
import { newClientId } from "@/api/transport"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"

const providers = ["feishu", "dingtalk", "smtp"] as const

export function NotificationTemplateAdmin({reason}: {reason: string}) {
  const queryClient = useQueryClient()
  const templates = useQuery({queryKey: ["notification-templates"], queryFn: getNotificationTemplates, retry: false})
  const destinations = useQuery({queryKey: ["notification-destinations"], queryFn: getNotificationDestinations, retry: false})
  const [provider, setProvider] = useState<(typeof providers)[number]>("feishu")
  const [eventType, setEventType] = useState("incident.opened")
  const [selected, setSelected] = useState<NotificationTemplate | null>(null)
  const [preview, setPreview] = useState<NotificationTemplatePreview | null>(null)
  const refresh = () => queryClient.invalidateQueries({queryKey: ["notification-templates"]})
  const mutation = useMutation({mutationFn: (work: () => Promise<unknown>) => work(), onSuccess: refresh})

  if (templates.isPending || destinations.isPending) return <div className="py-8 text-sm text-muted-foreground" role="status">正在加载 Notification Template</div>
  if (templates.isError || destinations.isError) return <div className="py-8 text-sm text-destructive">无法读取 Notification Template</div>
  const builtins = templates.data.templates.filter((item) => item.is_builtin)
  const custom = templates.data.templates.filter((item) => !item.is_builtin)
  const compatibleDestinations = destinations.data.destinations.filter((item) => item.enabled && item.provider === selected?.provider)
  const sample = selected ? sampleRequest(selected.event_type) : null

  return <section className="flex flex-col gap-4 border-t pt-6" aria-labelledby="template-heading">
    <div><h2 id="template-heading" className="text-lg font-semibold">Notification Template</h2><p className="text-sm text-muted-foreground">可用变量：{templates.data.variables.map((item) => `{{${item}}}`).join(" · ")}</p></div>
    {mutation.isError ? <Alert variant="destructive"><AlertTitle>Template 操作失败</AlertTitle><AlertDescription>{mutation.error instanceof Error ? mutation.error.message : "请求失败"}</AlertDescription></Alert> : null}
    <form className="grid gap-3 md:grid-cols-[180px_1fr_1fr_auto]" onSubmit={(event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget)
      const source = builtins.find((item) => item.provider === provider && item.event_type === eventType)
      if (source) mutation.mutate(() => copyNotificationTemplate({source_template_id: source.id, name: String(form.get("name") || ""), reason}))
    }}>
      <Field><FieldLabel htmlFor="template-provider">Provider</FieldLabel><Select value={provider} onValueChange={(value) => setProvider(value as typeof provider)}><SelectTrigger id="template-provider" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{providers.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
      <Field><FieldLabel htmlFor="template-event">Event</FieldLabel><Select value={eventType} onValueChange={(value) => setEventType(value ?? "incident.opened")}><SelectTrigger id="template-event" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{[...new Set(builtins.map((item) => item.event_type))].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
      <Field><FieldLabel htmlFor="template-name">副本名称</FieldLabel><Input id="template-name" name="name" required /></Field>
      <div className="flex items-end"><Button type="submit" disabled={!reason || mutation.isPending}><CopyIcon />复制内置模板</Button></div>
    </form>
    <Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>组合</TableHead><TableHead>Version</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{custom.map((item) => <TableRow key={item.id}><TableCell className="font-medium">{item.name}</TableCell><TableCell>{item.event_type} · {item.provider}</TableCell><TableCell>{item.version}</TableCell><TableCell><Badge variant={item.enabled ? "positive" : item.validated_at ? "outline" : "secondary"}>{item.enabled ? "启用" : item.validated_at ? "已预览" : "Draft"}</Badge></TableCell><TableCell><Button type="button" size="sm" variant="outline" onClick={() => {setSelected(item); setPreview(null)}}><PencilIcon />编辑</Button></TableCell></TableRow>)}</TableBody></Table>
    {selected ? <form key={`${selected.id}:${selected.version}`} className="grid gap-3 md:grid-cols-2" onSubmit={(event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget)
      mutation.mutate(async () => {const result = await updateNotificationTemplate(selected.id, {title: String(form.get("title")), body: String(form.get("body")), color: String(form.get("color")), button_label: String(form.get("button_label")), ...(selected.provider === "smtp" ? {subject: String(form.get("subject"))} : {}), reason}); setSelected(result.template)})
    }}>
      <Field><FieldLabel htmlFor="template-title">Title</FieldLabel><Input id="template-title" name="title" defaultValue={selected.title} required /></Field>
      {selected.provider === "smtp" ? <Field><FieldLabel htmlFor="template-subject">SMTP subject</FieldLabel><Input id="template-subject" name="subject" defaultValue={selected.subject ?? ""} required /></Field> : null}
      <Field className="md:col-span-2"><FieldLabel htmlFor="template-body">Markdown body</FieldLabel><Textarea id="template-body" name="body" defaultValue={selected.body} rows={8} required /></Field>
      <Field><FieldLabel htmlFor="template-color">Color</FieldLabel><Input id="template-color" name="color" type="color" defaultValue={selected.color} required /></Field>
      <Field><FieldLabel htmlFor="template-button">Button label</FieldLabel><Input id="template-button" name="button_label" defaultValue={selected.button_label} required /></Field>
      <div className="flex flex-wrap gap-2 md:col-span-2">
        <Button type="submit" disabled={!reason || mutation.isPending}>保存新版本</Button>
        <Button type="button" variant="outline" disabled={!reason || mutation.isPending || !sample} onClick={() => mutation.mutate(async () => {const result = await previewNotificationTemplate(selected.id, {request: sample!, reason}); setSelected(result.template); setPreview(result.preview)})}><FlaskConicalIcon />预览</Button>
        <Select onValueChange={(destinationId) => destinationId && sample && mutation.mutate(async () => {const result = await testNotificationTemplate(selected.id, {destination_id: String(destinationId), request: sample, reason}); setSelected(result.template); setPreview(result.preview)})}><SelectTrigger className="w-56" aria-label="Test destination"><SelectValue placeholder="发送 test delivery" /></SelectTrigger><SelectContent><SelectGroup>{compatibleDestinations.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectGroup></SelectContent></Select>
        <Button type="button" variant="outline" disabled={!reason || !selected.validated_at || mutation.isPending} onClick={() => mutation.mutate(async () => {const result = await updateNotificationTemplate(selected.id, {enabled: !selected.enabled, reason}); setSelected(result.template)})}>{selected.enabled ? "停用" : "启用"}</Button>
      </div>
      {preview ? <Alert className="md:col-span-2 border-l-4" style={{borderLeftColor: preview.color}}><FlaskConicalIcon /><AlertTitle>{preview.subject || preview.title}</AlertTitle><AlertDescription><div className="whitespace-pre-wrap">{preview.body}</div><Button type="button" size="sm" className="mt-3">{preview.button_label}</Button></AlertDescription></Alert> : null}
    </form> : null}
  </section>
}

export function sampleRequest(eventType: string) {
  const status = eventType.split(".")[1]
  const change = {incident_id: "preview", change_request_id: "preview", phase_id: "preview", status}
  const facts = eventType === "incident.severity_changed" ? {incident_id: "preview", status, previous_severity: "warning", severity: "critical"} : eventType.startsWith("incident.") ? {incident_id: "preview", status} : eventType.startsWith("investigation.") ? {incident_id: "preview", investigation_id: "preview", status, reason: "Preview"} : eventType === "change.awaiting_approval" ? change : eventType === "change.approved" ? {...change, approval_id: "preview"} : eventType === "change.effect_observed" || eventType === "change.reconciliation_accepted" ? {...change, reconciliation_id: "preview"} : eventType.startsWith("change.") ? {...change, execution_id: "preview"} : {connector_id: "preview", cluster_id: "preview", status}
  return {event_id: `preview:${newClientId()}`, event_type: eventType, occurred_at: Date.now() / 1000, severity: "critical", subject: {type: eventType.startsWith("change.") ? "change_request" : eventType.split(".")[0], id: "preview", version: 1}, scope: {environment: "prod", team_id: "preview", service_id: "preview"}, summary: "Checkout unavailable", facts, console_path: "/incidents/preview"}
}
