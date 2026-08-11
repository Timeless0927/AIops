import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CopyIcon, FlaskConicalIcon, PencilIcon } from "lucide-react"

import {
  copyNotificationTemplate, getNotificationDestinations, getNotificationTemplates,
  previewNotificationTemplate, testNotificationTemplate, updateNotificationTemplate,
  type NotificationTemplate,
  type NotificationTemplatePreview,
} from "@/admin/notification-client"
import { useAdminAction } from "@/admin/admin-action"
import { FormDialog } from "@/admin/admin-shared"
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

export function NotificationTemplateAdmin() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
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
    <div className="flex justify-end">
      <FormDialog
        trigger={<><CopyIcon data-icon="inline-start" />复制内置模板</>}
        title="复制内置模板"
        description="从 provider 与 event 的内置模板创建可编辑副本。"
        submitLabel="复制内置模板"
        pending={mutation.isPending}
        onSubmit={(form) => {
          const source = builtins.find((item) => item.provider === provider && item.event_type === eventType)
          const name = String(form.get("name") || "")
          if (source) requestAction({title: "复制 Notification Template", summary: `将从 ${source.name} 创建自定义模板 ${name}。`, run: (reason) => mutation.mutateAsync(() => copyNotificationTemplate({source_template_id: source.id, name, reason}))})
        }}
      >
        <Field><FieldLabel htmlFor="template-provider">Provider</FieldLabel><Select value={provider} onValueChange={(value) => setProvider(value as typeof provider)}><SelectTrigger id="template-provider" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{providers.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="template-event">Event</FieldLabel><Select value={eventType} onValueChange={(value) => setEventType(value ?? "incident.opened")}><SelectTrigger id="template-event" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{[...new Set(builtins.map((item) => item.event_type))].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="template-name">副本名称</FieldLabel><Input id="template-name" name="name" required /></Field>
      </FormDialog>
    </div>
    <Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>组合</TableHead><TableHead>Version</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{custom.map((item) => <TableRow key={item.id}><TableCell className="font-medium">{item.name}</TableCell><TableCell>{item.event_type} · {item.provider}</TableCell><TableCell>{item.version}</TableCell><TableCell><Badge variant={item.enabled ? "positive" : item.validated_at ? "outline" : "secondary"}>{item.enabled ? "启用" : item.validated_at ? "已预览" : "Draft"}</Badge></TableCell><TableCell><Button type="button" size="sm" variant="outline" onClick={() => {setSelected(item); setPreview(null)}}><PencilIcon />编辑</Button></TableCell></TableRow>)}</TableBody></Table>
    {selected ? <FormDialog
      key={`${selected.id}:${selected.version}`}
      open
      onOpenChange={(open) => { if (!open) { setSelected(null); setPreview(null) } }}
      title={`编辑 ${selected.name}`}
      description="保存新版本后重新预览和测试。"
      submitLabel="保存新版本"
      pending={mutation.isPending}
      contentClassName="sm:max-w-2xl"
      onSubmit={(form) => {
        const body = {title: String(form.get("title")), body: String(form.get("body")), color: String(form.get("color")), button_label: String(form.get("button_label")), ...(selected.provider === "smtp" ? {subject: String(form.get("subject"))} : {})}
        requestAction({title: "保存 Notification Template 新版本", summary: `将为 ${selected.name} 保存新的不可变模板版本。`, run: (reason) => mutation.mutateAsync(async () => {const result = await updateNotificationTemplate(selected.id, {...body, reason}); setSelected(result.template)})})
        return false
      }}
    >
      <div className="grid gap-3 md:grid-cols-2">
        <Field><FieldLabel htmlFor="template-title">Title</FieldLabel><Input id="template-title" name="title" defaultValue={selected.title} required /></Field>
        {selected.provider === "smtp" ? <Field><FieldLabel htmlFor="template-subject">SMTP subject</FieldLabel><Input id="template-subject" name="subject" defaultValue={selected.subject ?? ""} required /></Field> : null}
        <Field className="md:col-span-2"><FieldLabel htmlFor="template-body">Markdown body</FieldLabel><Textarea id="template-body" name="body" defaultValue={selected.body} rows={8} required /></Field>
        <Field><FieldLabel htmlFor="template-color">Color</FieldLabel><Input id="template-color" name="color" type="color" defaultValue={selected.color} required /></Field>
        <Field><FieldLabel htmlFor="template-button">Button label</FieldLabel><Input id="template-button" name="button_label" defaultValue={selected.button_label} required /></Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={mutation.isPending || !sample} onClick={() => requestAction({title: "预览 Notification Template", summary: `将使用样例事件预览 ${selected.name} 并标记当前版本已验证。`, run: (reason) => mutation.mutateAsync(async () => {const result = await previewNotificationTemplate(selected.id, {request: sample!, reason}); setSelected(result.template); setPreview(result.preview)})})}><FlaskConicalIcon />预览</Button>
        <Select onValueChange={(destinationId) => destinationId && sample && requestAction({title: "测试 Notification Template", summary: `将使用 ${selected.name} 向选定 Destination 发送 test delivery。`, run: (reason) => mutation.mutateAsync(async () => {const result = await testNotificationTemplate(selected.id, {destination_id: String(destinationId), request: sample, reason}); setSelected(result.template); setPreview(result.preview)})})}><SelectTrigger className="w-56" aria-label="Test destination"><SelectValue placeholder="发送 test delivery" /></SelectTrigger><SelectContent><SelectGroup>{compatibleDestinations.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectGroup></SelectContent></Select>
        <Button type="button" variant={selected.enabled ? "destructive" : "outline"} disabled={!selected.validated_at || mutation.isPending} onClick={() => requestAction({title: selected.enabled ? "停用 Notification Template" : "启用 Notification Template", summary: `${selected.enabled ? "将停用" : "将启用"}模板 ${selected.name}，影响引用它的后续 Route 投递。`, destructive: selected.enabled, run: (reason) => mutation.mutateAsync(async () => {const result = await updateNotificationTemplate(selected.id, {enabled: !selected.enabled, reason}); setSelected(result.template)})})}>{selected.enabled ? "停用" : "启用"}</Button>
      </div>
      {preview ? <Alert className="border-l-4" style={{borderLeftColor: preview.color}}><FlaskConicalIcon /><AlertTitle>{preview.subject || preview.title}</AlertTitle><AlertDescription><div className="whitespace-pre-wrap">{preview.body}</div><Button type="button" size="sm" className="mt-3">{preview.button_label}</Button></AlertDescription></Alert> : null}
    </FormDialog> : null}
  </section>
}

export function sampleRequest(eventType: string) {
  const status = eventType.split(".")[1]
  const change = {incident_id: "preview", change_request_id: "preview", phase_id: "preview", status}
  const facts = eventType === "incident.severity_changed" ? {incident_id: "preview", status, previous_severity: "warning", severity: "critical"} : eventType.startsWith("incident.") ? {incident_id: "preview", status} : eventType.startsWith("investigation.") ? {incident_id: "preview", investigation_id: "preview", status, reason: "Preview"} : eventType === "change.awaiting_approval" ? change : eventType === "change.approved" ? {...change, approval_id: "preview"} : eventType === "change.effect_observed" || eventType === "change.reconciliation_accepted" ? {...change, reconciliation_id: "preview"} : eventType.startsWith("change.") ? {...change, execution_id: "preview"} : {connector_id: "preview", cluster_id: "preview", status}
  return {event_id: `preview:${newClientId()}`, event_type: eventType, occurred_at: Date.now() / 1000, severity: "critical", subject: {type: eventType.startsWith("change.") ? "change_request" : eventType.split(".")[0], id: "preview", version: 1}, scope: {environment: "prod", team_id: "preview", service_id: "preview"}, summary: "Checkout unavailable", facts, console_path: "/incidents/preview"}
}
