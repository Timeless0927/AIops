import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ActivityIcon, FileTextIcon, PauseIcon, SendIcon, ServerIcon, ShieldCheckIcon, SquareIcon, UserRoundIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import {
  controlInvestigation,
  getIncidentWorkbench,
  listInvestigationEvents,
  reinvestigateIncident,
  submitHumanInput,
  type InvestigationEvent,
  type InvestigationEventsPage,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import { Textarea } from "@/components/ui/textarea"
import { appendInvestigationEvents } from "@/prototype/investigation-event-state"
import { ConsoleHeader, incidentLifecycleLabels, MonoValue } from "@/prototype/shared"

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
      idempotency_key: crypto.randomUUID(),
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
    },
  })
  const control = useMutation({
    mutationFn: (action: "pause" | "takeover" | "terminate") => controlInvestigation(investigationId, action),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
    },
  })
  const reinvestigate = useMutation({
    mutationFn: () => reinvestigateIncident(incidentId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
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

  if (workbench.isPending) return <div className="min-h-screen bg-background"><ConsoleHeader showBack /><LoadingState /></div>
  if (workbench.isError) return <div className="min-h-screen bg-background"><ConsoleHeader showBack /><ErrorState /></div>

  const snapshot = workbench.data
  const { incident, resource_context: resource, responsibility, investigation } = snapshot
  const canManage = snapshot.actor_capabilities.includes("manage_investigation")
  const isTerminal = investigation ? terminalStatuses.has(investigation.status) : false
  const connectionLabel = events.isError || connection === "denied" ? "无权访问" : connection === "live" ? "实时" : connection === "reconnecting" ? "正在重连" : connection === "terminal" ? "已结束" : "正在连接"

  return (
    <div className="min-h-screen bg-background pb-8">
      <ConsoleHeader showBack />
      <section className="border-b bg-surface" aria-labelledby="incident-title">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-4 py-5 lg:px-6">
          <div className="flex flex-wrap items-center gap-2">
            <MonoValue>{incident.id}</MonoValue>
            <Badge variant={incident.lifecycle_state === "resolved" ? "secondary" : "default"}>{incidentLifecycleLabels[incident.lifecycle_state]}</Badge>
            <Badge variant="outline">{bindingStatus[incident.binding_status]}</Badge>
            <Badge variant="secondary">{incident.severity}</Badge>
            <Link to={`/incidents/${incident.id}/report`} className="ml-auto inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
              <FileTextIcon className="size-4" />事件报告
            </Link>
          </div>
          <div>
            <h1 id="incident-title" className="text-2xl font-semibold">{incident.title}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {incident.alertname} · {incident.signal_count} 个 Alert Signal
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
                <dt className="text-xs text-muted-foreground">Cluster / Namespace</dt>
                <dd className="mt-1 break-words font-medium">{resource.cluster_name} / {resource.namespace}</dd>
                <dd className="mt-1"><MonoValue>{resource.environment} · {resource.runtime_status}</MonoValue></dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">Deployment Target</dt>
                <dd className="mt-1 break-words font-medium">{resource.workload_name ?? "无法解析"}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">{resource.workload_kind ?? "未知工作负载类型"}</dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">Service / Team</dt>
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
                  Evidence revision {incident.evidence_revision}
                </dd>
              </div>
            </dl>
            <div className="mt-5 flex items-center gap-2 text-xs text-muted-foreground">
              <ServerIcon className="size-4" />快照 r{snapshot.snapshot_revision} · cursor {snapshot.event_cursor}
            </div>
          </aside>

          <div className="min-w-0">
          <section className="border-b" aria-labelledby="timeline-title">
            <header className="flex flex-wrap items-center gap-2 border-b p-4">
              <div>
                <h2 id="timeline-title" className="text-base font-semibold">Investigation Events</h2>
                <p className="mt-1 text-xs text-muted-foreground">cursor {events.data?.next_cursor ?? snapshot.event_cursor}</p>
              </div>
              <Badge className="ml-auto" variant={connection === "live" ? "default" : "secondary"}>{connectionLabel}</Badge>
              {canManage && investigation && !isTerminal ? <>
                <Button size="sm" variant="outline" onClick={() => control.mutate("pause")} disabled={control.isPending}><PauseIcon />暂停</Button>
                <Button size="sm" variant="outline" onClick={() => control.mutate("takeover")} disabled={control.isPending}><UserRoundIcon />人工接管</Button>
                <Button size="sm" variant="destructive" onClick={() => control.mutate("terminate")} disabled={control.isPending}><SquareIcon />终止</Button>
              </> : null}
              {canManage && isTerminal ? <Button size="sm" onClick={() => reinvestigate.mutate()} disabled={reinvestigate.isPending}><ActivityIcon />重新调查</Button> : null}
            </header>
            <div className="max-h-[420px] divide-y overflow-y-auto" aria-live="polite">
              {events.isPending ? <p className="p-4 text-sm text-muted-foreground">正在加载进展</p> : null}
              {events.isError ? <p className="p-4 text-sm text-destructive">当前账号无法读取调查进展。</p> : null}
              {events.data?.events.map((event) => (
                <article key={event.id} className="flex gap-3 p-4">
                  <MonoValue>#{event.id}</MonoValue>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{eventLabels[event.type] ?? event.type}</span>
                      <time className="text-xs text-muted-foreground">{new Date(event.created_at * 1000).toLocaleString("zh-CN")}</time>
                    </div>
                    <p className="mt-1 break-words text-sm text-muted-foreground">{eventSummary(event)}</p>
                  </div>
                  {canManage && event.type.startsWith("human_input.") ? <div className="flex shrink-0 gap-1">
                    <Button size="xs" variant="ghost" onClick={() => { setInputKind("correction"); setTargetEventId(event.id) }}>修正</Button>
                    <Button size="xs" variant="ghost" onClick={() => { setInputKind("retraction"); setTargetEventId(event.id) }}>撤回</Button>
                  </div> : null}
                </article>
              ))}
            </div>
            {canManage && investigation && (!isTerminal || inputKind !== "assertion") ? <form className="border-t p-4" onSubmit={(submitEvent) => { submitEvent.preventDefault(); input.mutate() }}>
              <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="outline">{inputKind === "assertion" ? "新增输入" : inputKind === "correction" ? `修正 #${targetEventId}` : `撤回 #${targetEventId}`}</Badge>
                {inputKind !== "assertion" ? <Button type="button" size="xs" variant="ghost" onClick={() => { setInputKind("assertion"); setTargetEventId(undefined) }}>取消</Button> : null}
              </div>
              <div className="flex items-end gap-2">
                <Textarea value={content} onChange={(event) => setContent(event.target.value)} maxLength={4000} required aria-label="Human Input" />
                <Button type="submit" size="icon" disabled={!content.trim() || input.isPending} title="提交 Human Input"><SendIcon /><span className="sr-only">提交 Human Input</span></Button>
              </div>
              {input.isError ? <p className="mt-2 text-xs text-destructive">提交失败，请检查调查状态后重试。</p> : null}
            </form> : null}
          </section>

          <section aria-labelledby="signals-title">
            <header className="border-b p-4">
              <h2 id="signals-title" className="text-base font-semibold">Alert Signals</h2>
              <p className="mt-1 text-xs text-muted-foreground">独立保留每个 Alertmanager fingerprint</p>
            </header>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Alert</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">Fingerprint</TableHead>
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
    </div>
  )
}
