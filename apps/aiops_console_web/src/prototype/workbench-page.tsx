import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ActivityIcon, FileTextIcon, PauseIcon, SearchCheckIcon, SendIcon, ServerIcon, ShieldCheckIcon, SquareIcon, UserRoundIcon, WrenchIcon, ZapIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import {
  controlInvestigation,
  approveAndExecute,
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
import { ChangeRequestsSection } from "@/changes/change-requests-section"
import { appendInvestigationEvents } from "@/prototype/investigation-event-state"
import { incidentLifecycleLabels, MonoValue } from "@/prototype/shared"

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
const executionStatus = {
  queued: "等待 Connector",
  leased: "已领取",
  started: "执行中",
  succeeded: "执行成功",
  failed: "执行失败",
  rejected: "已拒绝",
  unknown_outcome: "结果未知",
}

function parameterSummary(parameters: Record<string, unknown>) {
  const entries = Object.entries(parameters)
  return entries.length ? entries.map(([key, value]) => `${key}: ${String(value)}`).join(" · ") : "无需参数"
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
  const approval = useMutation({
    mutationFn: (action: {id: string; version: number; hash: string}) => approveAndExecute(incidentId, action.id, action.version, action.hash),
    onSuccess: async () => queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]}),
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

          <section className="border-b" aria-labelledby="evidence-title">
            <header className="flex items-center gap-3 border-b p-4">
              <SearchCheckIcon className="size-5 text-muted-foreground" />
              <div>
                <h2 id="evidence-title" className="text-base font-semibold">Evidence Steps</h2>
                <p className="mt-1 text-xs text-muted-foreground">{snapshot.evidence_steps.length} 个证据获取步骤</p>
              </div>
            </header>
            {snapshot.evidence_steps.length ? <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>目的 / 来源</TableHead>
                  <TableHead>Scope</TableHead>
                  <TableHead>结果 / 影响</TableHead>
                  <TableHead className="text-right">状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.evidence_steps.map((step) => (
                  <TableRow key={step.id}>
                    <TableCell className="max-w-[320px] whitespace-normal py-3 align-top">
                      <div className="font-medium">{step.purpose}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{step.source} · {step.evidence_references.join(" · ") || "无 evidence reference"}</div>
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
            </Table> : <p className="p-4 text-sm text-muted-foreground">尚无 Evidence Step</p>}
          </section>

          <section className="border-b" aria-labelledby="judgment-title">
            <header className="flex flex-wrap items-center gap-3 border-b p-4">
              <ShieldCheckIcon className="size-5 text-muted-foreground" />
              <h2 id="judgment-title" className="text-base font-semibold">当前判断</h2>
              {snapshot.judgment ? <Badge className="ml-auto" variant={snapshot.judgment.evidence_gate_status === "complete" && snapshot.judgment.valid ? "default" : "outline"}>
                {snapshot.judgment.valid ? (snapshot.judgment.evidence_gate_status === "complete" ? "Evidence Gate 完整" : "Evidence Gate 不完整") : "判断已失效"}
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

          <section className="border-b" aria-labelledby="actions-title">
            <header className="flex items-center gap-3 border-b p-4">
              <WrenchIcon className="size-5 text-muted-foreground" />
              <div>
                <h2 id="actions-title" className="text-base font-semibold">Recommended Actions</h2>
                <p className="mt-1 text-xs text-muted-foreground">{snapshot.recommended_actions.length} 个冻结版本</p>
              </div>
            </header>
            {snapshot.recommended_actions.length ? <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>动作</TableHead>
                  <TableHead>目标 / 参数</TableHead>
                  <TableHead>Safeguards</TableHead>
                  <TableHead className="text-right">审批</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.recommended_actions.map((action) => (
                  <TableRow key={`${action.id}:${action.version}`}>
                    <TableCell className="max-w-[360px] whitespace-normal py-3 align-top">
                      <div className="font-medium">{action.summary}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{action.action_type} · v{action.version} · <MonoValue>{action.hash.slice(0, 12)}</MonoValue></div>
                    </TableCell>
                    <TableCell className="max-w-[300px] whitespace-normal align-top text-xs">
                      {action.target.cluster_id} / {action.target.namespace} / {action.target.workload_name ?? "未解析"}<br />
                      <span className="text-muted-foreground">{parameterSummary(action.parameters)}</span>
                    </TableCell>
                    <TableCell className="max-w-[320px] whitespace-normal align-top text-xs">{action.safeguards.join(" · ") || "未提供"}</TableCell>
                    <TableCell className="max-w-[300px] whitespace-normal text-right align-top">
                      <Badge variant={action.gate.approvable && !action.stale ? "default" : "outline"}>{action.stale ? "已过期" : action.gate.approvable ? "可审批" : "不可审批"}</Badge>
                      {action.gate.reasons.length ? <div className="mt-2 text-xs text-muted-foreground">{action.gate.reasons.join(" · ")}</div> : null}
                      {action.approval_id ? <div className="mt-2 text-xs text-muted-foreground">已批准 · <MonoValue>{action.approval_id}</MonoValue></div> : null}
                      {action.execution ? <div className="mt-2 space-y-1 text-xs">
                        <Badge variant={action.execution.status === "unknown_outcome" || action.execution.status === "failed" ? "destructive" : "secondary"}>
                          {executionStatus[action.execution.status]}
                        </Badge>
                        {action.execution.result?.error_code ? <div className="text-muted-foreground">{action.execution.result.error_code}</div> : null}
                      </div> : null}
                      {action.can_approve ? <details className="mt-3 text-left">
                        <summary className="cursor-pointer text-xs font-medium">审阅冻结动作</summary>
                        <div className="mt-2 space-y-2 border-l-2 pl-3 text-xs">
                          <div>目标：{action.target.cluster_id}/{action.target.namespace}/{action.target.workload_kind}/{action.target.workload_name}</div>
                          <div>参数：{parameterSummary(action.parameters)}</div>
                          <div>Evidence：{action.evidence_step_ids.join(" · ")}</div>
                          <div>Safeguards：{action.safeguards.join(" · ")}</div>
                          <div>有效期：{new Date(action.expires_at * 1000).toLocaleString()}</div>
                          <div>Rollback Plan：{action.rollback_plan ? JSON.stringify(action.rollback_plan) : "无"}</div>
                          <Button className="mt-1" size="sm" variant="destructive" disabled={approval.isPending} onClick={() => approval.mutate(action)}><ZapIcon />批准并执行</Button>
                        </div>
                      </details> : null}
                      {approval.isError && action.can_approve ? <div className="mt-2 text-xs text-destructive">审批失败，请刷新后重新审阅。</div> : null}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table> : <p className="p-4 text-sm text-muted-foreground">尚无 Recommended Action</p>}
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
