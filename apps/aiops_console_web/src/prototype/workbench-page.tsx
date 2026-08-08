import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ActivityIcon, FileTextIcon, MessageSquareTextIcon, PauseIcon, SearchCheckIcon, SendIcon, ServerIcon, Settings2Icon, ShieldCheckIcon, SquareIcon, UserRoundIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import {
  controlInvestigation,
  getIncidentWorkbench,
  listInvestigationEvents,
  newClientId,
  reinvestigateIncident,
  submitHumanInput,
  type InvestigationEvent,
  type InvestigationEventsPage,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Textarea } from "@/components/ui/textarea"
import { ChangeRequestsSection } from "@/changes/change-requests-section"
import { DecisionTrace } from "@/prototype/decision-trace"
import { appendInvestigationEvents } from "@/prototype/investigation-event-state"
import { DiagnosisStatusBadge, incidentLifecycleLabels, MonoValue } from "@/prototype/shared"
import { RecommendationsSection } from "@/recommendations/recommendations-section"

const bindingStatus = {bound: "已绑定资源", unbound: "资源未绑定"}
const signalStatus = {firing: "告警中", recovered: "已恢复"}
const investigationStatus = {
  queued: "等待调查",
  running: "调查中",
  paused: "已暂停",
  human_led: "人工接管",
  completed: "已完成",
  failed: "失败",
  terminated: "已终止",
}
const eventLabels: Record<string, string> = {
  "investigation.lifecycle": "调查状态",
  "human_input.assertion": "人工输入",
  "human_input.correction": "输入修正",
  "human_input.retraction": "输入撤回",
  "diagnosis.output": "诊断判断",
  "tool.activity": "工具活动",
  "evidence_step.changed": "证据步骤",
  "judgment.invalidated": "判断已失效",
  "recommended_action.stale": "建议动作已过期",
}
const terminalStatuses = new Set(["completed", "failed", "terminated"])
const evidenceStatus = {
  running: "采集中",
  succeeded: "已取得",
  partial: "部分取得",
  failed: "失败",
  skipped: "已跳过",
}
function eventSummary(event: InvestigationEvent) {
  const payload = event.payload
  if (event.type.startsWith("human_input.")) {
    const target = typeof payload.target_event_id === "number" ? ` · 引用 #${payload.target_event_id}` : ""
    return `${String(payload.content ?? "")}${target}`
  }
  if (event.type === "investigation.lifecycle") return `${String(payload.from ?? "开始")} → ${String(payload.to ?? "")}`
  if (event.type === "diagnosis.output") {
    const diagnosis = payload.diagnosis
    return typeof diagnosis === "object" && diagnosis && "summary" in diagnosis ? String(diagnosis.summary) : String(payload.status ?? "")
  }
  if (event.type === "recommended_action.stale") return String(payload.recommended_action_id ?? "")
  return String(payload.summary ?? payload.reason ?? "状态已更新")
}

type EventAttachment = {
  source_attachment_id?: unknown
  retained_reference_id?: unknown
  filename?: unknown
  content_type?: unknown
  size?: unknown
  sha256?: unknown
}

function eventAttachments(event: InvestigationEvent): EventAttachment[] {
  return Array.isArray(event.payload.attachments)
    ? event.payload.attachments.filter((item): item is EventAttachment => typeof item === "object" && item !== null)
    : []
}

export function InvestigationEventAccordion({
  events,
  canManage,
  onCorrect = () => undefined,
  onRetract = () => undefined,
}: {
  events: InvestigationEvent[]
  canManage: boolean
  onCorrect?: (eventId: number) => void
  onRetract?: (eventId: number) => void
}) {
  return (
    <Accordion keepMounted className="divide-y" aria-label="调查事件详情">
      {events.map((event) => {
        const attachments = eventAttachments(event)
        return <AccordionItem key={event.id} value={String(event.id)} className="border-0">
          <AccordionTrigger>
            <span className="flex min-w-0 flex-1 gap-3">
              <MonoValue>#{event.id}</MonoValue>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{eventLabels[event.type] ?? "调查事件"}</span>
                  <time className="text-xs text-muted-foreground">{new Date(event.created_at * 1000).toLocaleString("zh-CN")}</time>
                </span>
                <span className="mt-1 block break-words text-sm text-muted-foreground">{eventSummary(event)}</span>
              </span>
            </span>
          </AccordionTrigger>
          <AccordionContent>
            <dl className="grid gap-2 border-t pt-3 text-xs text-muted-foreground sm:grid-cols-2">
              <div><dt className="font-medium text-foreground">事件类型</dt><dd>{eventLabels[event.type] ?? "调查事件"}</dd></div>
              <div><dt className="font-medium text-foreground">提交者</dt><dd className="break-all">{event.actor_id ?? "系统"}</dd></div>
              {event.payload.source ? <div><dt className="font-medium text-foreground">来源</dt><dd>{event.payload.source === "chat_handoff" ? "AI 对话 Handoff" : String(event.payload.source)}</dd></div> : null}
              {event.payload.content_sha256 ? <div><dt className="font-medium text-foreground">内容 SHA-256</dt><dd className="break-all font-mono">{String(event.payload.content_sha256)}</dd></div> : null}
            </dl>
            {attachments.length ? <div className="mt-3 border-t pt-3 text-xs">
              <p className="font-medium">保留附件</p>
              <ul className="mt-2 space-y-2 text-muted-foreground">
                {attachments.map((attachment, index) => <li key={String(attachment.retained_reference_id ?? attachment.source_attachment_id ?? index)} className="break-words">
                  <span className="font-medium text-foreground">{String(attachment.filename ?? "附件")}</span>
                  <span> · {String(attachment.content_type ?? "未知类型")} · {String(attachment.size ?? 0)} 字节</span>
                  {attachment.sha256 ? <span className="mt-1 block break-all font-mono">SHA-256 {String(attachment.sha256)}</span> : null}
                </li>)}
              </ul>
            </div> : null}
            {canManage && event.type.startsWith("human_input.") ? <div className="mt-3 flex gap-2 border-t pt-3">
              <Button size="xs" variant="outline" onClick={() => onCorrect(event.id)}>修正此输入</Button>
              <Button size="xs" variant="ghost" onClick={() => onRetract(event.id)}>撤回此输入</Button>
            </div> : null}
          </AccordionContent>
        </AccordionItem>
      })}
    </Accordion>
  )
}

function LoadingState() {
  return <main className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground" role="status">正在加载事件</main>
}

function ErrorState() {
  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8 lg:px-6">
      <Empty className="min-h-72 border">
        <EmptyHeader>
          <EmptyMedia variant="icon"><ActivityIcon /></EmptyMedia>
          <EmptyTitle>无法加载事件</EmptyTitle>
          <EmptyDescription>事件不存在，或当前账号没有访问权限。</EmptyDescription>
        </EmptyHeader>
      </Empty>
    </main>
  )
}

export function WorkbenchPrototypePage() {
  const { incidentId = "" } = useParams()
  const queryClient = useQueryClient()
  const [connection, setConnection] = useState<"connecting" | "live" | "reconnecting" | "terminal" | "denied">("connecting")
  const [handoff, setHandoff] = useState({investigationId: "", cursor: 0})
  const [inputKind, setInputKind] = useState<"assertion" | "correction" | "retraction">("assertion")
  const [targetEventId, setTargetEventId] = useState<number | undefined>()
  const [content, setContent] = useState("")
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [controlsOpen, setControlsOpen] = useState(false)
  const [terminateOpen, setTerminateOpen] = useState(false)
  const workbench = useQuery({
    queryKey: ["incidents", incidentId, "workbench"],
    queryFn: () => getIncidentWorkbench(incidentId),
    enabled: Boolean(incidentId),
  })
  const investigationId = workbench.data?.investigation?.id ?? ""
  const eventKey = ["investigations", investigationId, "events"] as const
  const events = useQuery({
    queryKey: eventKey,
    queryFn: () => listInvestigationEvents(investigationId),
    enabled: Boolean(investigationId),
  })
  const input = useMutation({
    mutationFn: () => submitHumanInput(investigationId, {
      kind: inputKind,
      content,
      idempotency_key: newClientId(),
      ...(targetEventId ? {target_event_id: targetEventId} : {}),
    }),
    onSuccess: ({event}) => {
      queryClient.setQueryData<InvestigationEventsPage>(eventKey, (current) => current && ({
        ...current,
        events: appendInvestigationEvents(current.events, [event]),
        next_cursor: Math.max(current.next_cursor, event.id),
      }))
      setContent("")
      setInputKind("assertion")
      setTargetEventId(undefined)
      setFeedbackOpen(false)
    },
  })
  const control = useMutation({
    mutationFn: (action: "pause" | "takeover" | "terminate") => controlInvestigation(investigationId, action),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
      setControlsOpen(false)
    },
  })
  const reinvestigate = useMutation({
    mutationFn: () => reinvestigateIncident(incidentId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
      setControlsOpen(false)
    },
  })
  useEffect(() => {
    if (investigationId && workbench.data?.event_cursor !== undefined) {
      setHandoff((current) => current.investigationId === investigationId
        ? current
        : {investigationId, cursor: workbench.data!.event_cursor})
    }
  }, [investigationId, workbench.data?.event_cursor])

  useEffect(() => {
    const current = workbench.data
    if (!investigationId || handoff.investigationId !== investigationId || !current) return
    setConnection(terminalStatuses.has(current.investigation?.status ?? "") ? "terminal" : "connecting")
    if (terminalStatuses.has(current.investigation?.status ?? "")) return
    const source = new EventSource(
      `/api/v1/investigations/${encodeURIComponent(investigationId)}/events/stream?after=${handoff.cursor}`,
    )
    source.onopen = () => setConnection("live")
    source.addEventListener("investigation", (message) => {
      const event = JSON.parse(message.data) as InvestigationEvent
      queryClient.setQueryData<InvestigationEventsPage>(eventKey, (current) => current && ({
        ...current,
        events: appendInvestigationEvents(current.events, [event]),
        next_cursor: Math.max(current.next_cursor, event.id),
      }))
      if (event.type !== "human_input.assertion" && event.type !== "human_input.correction" && event.type !== "human_input.retraction") {
        void queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
      }
    })
    source.addEventListener("terminal", () => {
      setConnection("terminal")
      source.close()
      void queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
    })
    source.addEventListener("permission_denied", () => {
      setConnection("denied")
      source.close()
    })
    source.onerror = () => setConnection("reconnecting")
    return () => source.close()
  }, [handoff, incidentId, investigationId, queryClient, workbench.data?.investigation?.status])

  if (workbench.isPending) return <LoadingState />
  if (workbench.isError) return <ErrorState />

  const snapshot = workbench.data
  const { incident, resource_context: resource, responsibility, investigation } = snapshot
  const canManage = snapshot.actor_capabilities.includes("manage_investigation")
  const isTerminal = investigation ? terminalStatuses.has(investigation.status) : false
  const connectionLabel = events.isError || connection === "denied" ? "无权访问" : connection === "live" ? "实时" : connection === "reconnecting" ? "正在重连" : connection === "terminal" ? "已结束" : "正在连接"

  return (
    <div className="pb-8">
      <section className="border-b bg-surface" aria-labelledby="incident-title">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-4 py-5 lg:px-6">
          <div className="flex flex-wrap items-center gap-2">
            <MonoValue>{incident.id}</MonoValue>
            <Badge variant={incident.lifecycle_state === "resolved" ? "secondary" : "default"}>{incidentLifecycleLabels[incident.lifecycle_state]}</Badge>
            <DiagnosisStatusBadge {...incident} />
            <Badge variant="outline">{bindingStatus[incident.binding_status]}</Badge>
            <Badge variant="secondary">{incident.severity}</Badge>
            <Link to={`/incidents/${incident.id}/report`} className="ml-auto inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
              <FileTextIcon className="size-4" />事件报告
            </Link>
          </div>
          <div>
            <h1 id="incident-title" className="text-2xl font-semibold">{incident.title}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {incident.alertname} · {incident.signal_count} 个告警信号（Alert Signal）
            </p>
          </div>
        </div>
      </section>

      <main className="mx-auto max-w-[1500px] px-4 py-5 lg:px-6">
        <div className="overflow-hidden rounded-md border lg:grid lg:grid-cols-[300px_minmax(0,1fr)]">
          <aside className="border-b bg-surface p-4 lg:border-r lg:border-b-0" aria-labelledby="facts-title">
            <h2 id="facts-title" className="text-sm font-medium">事件事实</h2>
            <dl className="mt-3 divide-y text-sm">
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">集群（Cluster）/ Namespace</dt>
                <dd className="mt-1 break-words font-medium">{resource.cluster_name} / {resource.namespace}</dd>
                <dd className="mt-1"><MonoValue>{resource.environment} · {resource.runtime_status}</MonoValue></dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">部署目标（Deployment Target）</dt>
                <dd className="mt-1 break-words font-medium">{resource.workload_name ?? "无法解析"}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">{resource.workload_kind ?? "未知工作负载类型"}</dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">服务（Service）/ 团队（Team）</dt>
                <dd className="mt-1 break-words font-medium">{resource.service_name ?? "未绑定 Service"}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">{responsibility.team_name ?? "责任团队待确认"}</dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">调查状态</dt>
                <dd className="mt-1 flex items-center gap-2 font-medium">
                  <ShieldCheckIcon className="size-4 text-muted-foreground" />
                  {investigation ? investigationStatus[investigation.status] : "尚未建立调查"}
                </dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">恢复状态</dt>
                <dd className="mt-1 font-medium">{incidentLifecycleLabels[incident.lifecycle_state]}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">
                  证据版本 {incident.evidence_revision}
                </dd>
              </div>
            </dl>
            <div className="mt-5 flex items-center gap-2 text-xs text-muted-foreground">
              <ServerIcon className="size-4" />快照 r{snapshot.snapshot_revision} · 游标 {snapshot.event_cursor}
            </div>
          </aside>

          <div className="min-w-0">
          <section className="border-b" aria-labelledby="timeline-title">
            <header className="flex flex-wrap items-center gap-2 border-b p-4">
              <div>
                <h2 id="timeline-title" className="text-base font-semibold">调查事件</h2>
                <p className="mt-1 text-xs text-muted-foreground">游标 {events.data?.next_cursor ?? snapshot.event_cursor}</p>
              </div>
              <Badge className="ml-auto" variant={connection === "live" ? "default" : "secondary"}>{connectionLabel}</Badge>
              {canManage && investigation && !isTerminal ? <Button size="sm" variant="outline" onClick={() => { setInputKind("assertion"); setTargetEventId(undefined); setFeedbackOpen(true) }}><MessageSquareTextIcon />提供反馈</Button> : null}
              {canManage && investigation ? <Sheet open={controlsOpen} onOpenChange={setControlsOpen}>
                <SheetTrigger render={<Button size="sm" variant="outline" />}><Settings2Icon />调查控制</SheetTrigger>
                <SheetContent className="w-[min(24rem,90vw)]">
                  <SheetHeader><SheetTitle>调查控制</SheetTitle><SheetDescription>当前状态：{investigationStatus[investigation.status]}</SheetDescription></SheetHeader>
                  <div className="grid gap-2 px-4">
                    {!isTerminal && ["queued", "running"].includes(investigation.status) ? <Button variant="outline" onClick={() => control.mutate("pause")} disabled={control.isPending}><PauseIcon />暂停调查</Button> : null}
                    {!isTerminal && ["queued", "running", "paused"].includes(investigation.status) ? <Button variant="outline" onClick={() => control.mutate("takeover")} disabled={control.isPending}><UserRoundIcon />人工接管</Button> : null}
                    {!isTerminal ? <Button variant="destructive" onClick={() => { setControlsOpen(false); setTerminateOpen(true) }} disabled={control.isPending}><SquareIcon />终止调查</Button> : null}
                    {isTerminal ? <Button onClick={() => reinvestigate.mutate()} disabled={reinvestigate.isPending}><ActivityIcon />重新调查</Button> : null}
                    {control.isError || reinvestigate.isError ? <p className="text-sm text-destructive" role="alert">操作失败，请刷新调查状态后重试。</p> : null}
                  </div>
                </SheetContent>
              </Sheet> : null}
            </header>
            <div className="max-h-[420px] overflow-y-auto" aria-live="polite">
              {events.isPending ? <p className="p-4 text-sm text-muted-foreground">正在加载进展</p> : null}
              {events.isError ? <p className="p-4 text-sm text-destructive">当前账号无法读取调查进展。</p> : null}
              <InvestigationEventAccordion
                events={events.data?.events ?? []}
                canManage={canManage}
                onCorrect={(eventId) => { setInputKind("correction"); setTargetEventId(eventId); setFeedbackOpen(true) }}
                onRetract={(eventId) => { setInputKind("retraction"); setTargetEventId(eventId); setFeedbackOpen(true) }}
              />
            </div>
          </section>

          <DecisionTrace events={events.data?.events ?? []} judgment={snapshot.judgment} />

          <section className="border-b" aria-labelledby="evidence-title">
            <header className="flex items-center gap-3 border-b p-4">
              <SearchCheckIcon className="size-5 text-muted-foreground" />
              <div>
                <h2 id="evidence-title" className="text-base font-semibold">证据步骤（Evidence Steps）</h2>
                <p className="mt-1 text-xs text-muted-foreground">{snapshot.evidence_steps.length} 个证据获取步骤</p>
              </div>
            </header>
            {snapshot.evidence_steps.length ? <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>目的 / 来源</TableHead>
                  <TableHead>范围（Scope）</TableHead>
                  <TableHead>结果 / 影响</TableHead>
                  <TableHead className="text-right">状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.evidence_steps.map((step) => (
                  <TableRow key={step.id}>
                    <TableCell className="max-w-[320px] whitespace-normal py-3 align-top">
                      <div className="font-medium">{step.purpose}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{step.source} · {step.evidence_references.join(" · ") || "无 Evidence 引用"}</div>
                    </TableCell>
                    <TableCell className="max-w-[260px] whitespace-normal align-top text-xs">
                      {step.scope.cluster_id} / {step.scope.namespace}<br />
                      {step.scope.workload_kind}/{step.scope.workload_name}
                    </TableCell>
                    <TableCell className="max-w-[420px] whitespace-normal align-top">
                      <div className="text-sm">{step.result ?? step.missing_guidance}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{step.impact}</div>
                      {step.missing_guidance ? <div className="mt-1 text-xs text-destructive">{step.missing_guidance}</div> : null}
                    </TableCell>
                    <TableCell className="text-right align-top"><Badge variant={step.state === "succeeded" ? "secondary" : "outline"}>{evidenceStatus[step.state]}</Badge></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table> : <p className="p-4 text-sm text-muted-foreground">尚无证据步骤（Evidence Step）</p>}
          </section>

          <section className="border-b" aria-labelledby="judgment-title">
            <header className="flex flex-wrap items-center gap-3 border-b p-4">
              <ShieldCheckIcon className="size-5 text-muted-foreground" />
              <h2 id="judgment-title" className="text-base font-semibold">当前判断</h2>
              {snapshot.judgment ? <Badge className="ml-auto" variant={snapshot.judgment.evidence_gate_status === "complete" && snapshot.judgment.valid ? "default" : "outline"}>
                {snapshot.judgment.valid ? (snapshot.judgment.evidence_gate_status === "complete" ? "诊断证据完整" : "诊断证据不足") : "判断已失效"}
              </Badge> : null}
            </header>
            {snapshot.judgment ? <div className="p-4">
              <p className="text-sm">{snapshot.judgment.summary}</p>
              {snapshot.judgment.next_evidence_guidance.length ? <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                {snapshot.judgment.next_evidence_guidance.map((guidance) => <li key={guidance}>{guidance}</li>)}
              </ul> : null}
            </div> : <p className="p-4 text-sm text-muted-foreground">尚无诊断判断</p>}
          </section>

          <ChangeRequestsSection incidentId={incidentId} changeRequests={snapshot.change_requests} canManage={canManage} />
          <RecommendationsSection
            incidentId={incidentId}
            recommendations={snapshot.recommended_actions}
            canManage={canManage}
          />

          <section aria-labelledby="signals-title">
            <header className="border-b p-4">
              <h2 id="signals-title" className="text-base font-semibold">告警信号（Alert Signals）</h2>
              <p className="mt-1 text-xs text-muted-foreground">独立保留每个 Alertmanager 指纹</p>
            </header>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>告警</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">指纹</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.alert_signals.map((signal) => (
                  <TableRow key={signal.fingerprint}>
                    <TableCell className="max-w-[520px] whitespace-normal py-3">
                      <div className="font-medium">{signal.alertname}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{signal.summary || "无摘要"}</div>
                    </TableCell>
                    <TableCell className="whitespace-normal">{signal.workload_name ?? "未解析"}</TableCell>
                    <TableCell><Badge variant={signal.status === "firing" ? "destructive" : "secondary"}>{signalStatus[signal.status]}</Badge></TableCell>
                    <TableCell className="text-right"><MonoValue>{signal.fingerprint}</MonoValue></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
          </div>
        </div>
      </main>
      <Dialog open={feedbackOpen} onOpenChange={setFeedbackOpen}>
        <DialogContent>
          <form onSubmit={(submitEvent) => { submitEvent.preventDefault(); input.mutate() }}>
            <DialogHeader>
              <DialogTitle>{inputKind === "assertion" ? "提供调查反馈" : inputKind === "correction" ? `修正输入 #${targetEventId}` : `撤回输入 #${targetEventId}`}</DialogTitle>
              <DialogDescription>反馈属于 Human Input，不会自动成为 Evidence、Approval 或执行授权。</DialogDescription>
            </DialogHeader>
            <Textarea className="mt-4" value={content} onChange={(event) => setContent(event.target.value)} maxLength={4000} required aria-label="Human Input" placeholder="输入需要调查继续核实的信息" />
            {input.isError ? <p className="mt-2 text-xs text-destructive" role="alert">提交失败，请检查调查状态后重试。</p> : null}
            <DialogFooter className="mt-4">
              <DialogClose render={<Button type="button" variant="outline" />}>取消</DialogClose>
              <Button type="submit" disabled={!content.trim() || input.isPending}><SendIcon />提交反馈</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <AlertDialog open={terminateOpen} onOpenChange={setTerminateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>终止当前调查？</AlertDialogTitle>
            <AlertDialogDescription>终止后当前 Investigation 不再接收新的 assertion；需要继续时可重新调查。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => control.mutate("terminate")}>确认终止</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
