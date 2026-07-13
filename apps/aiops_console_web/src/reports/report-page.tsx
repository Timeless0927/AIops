import { useState } from "react"
import { FileCheck2Icon, LockKeyholeIcon, SaveIcon } from "lucide-react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useParams } from "react-router"

import {
  getIncidentReport,
  publishIncidentReport,
  updateIncidentReport,
  type IncidentReportDraft,
  type IncidentReportNarrative,
} from "@/api/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import { MonoValue } from "@/prototype/shared"

const narrativeLabels: Record<keyof IncidentReportNarrative, string> = {
  impact: "影响范围",
  root_cause: "根因说明",
  resolution_summary: "解决摘要",
  follow_up: "后续事项",
}

export function IncidentReportPage() {
  const { incidentId = "" } = useParams()
  const report = useQuery({
    queryKey: ["incidents", incidentId, "report"],
    queryFn: () => getIncidentReport(incidentId),
    enabled: Boolean(incidentId),
  })

  if (report.isPending) return <PageShell><PageStatus>正在加载报告</PageStatus></PageShell>
  if (report.isError) return <PageShell><PageStatus>无法读取事件报告</PageStatus></PageShell>

  return (
    <PageShell>
      <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-5">
        <div className="flex flex-col gap-1">
          <p className="font-mono text-xs text-muted-foreground">{incidentId}</p>
          <h1 className="text-2xl font-semibold">事件报告</h1>
        </div>
        <Badge variant={report.data.draft?.status === "published" ? "positive" : "outline"}>
          {report.data.draft?.status === "published" ? "已发布" : report.data.availability === "ready" ? "草稿" : "尚未就绪"}
        </Badge>
      </header>

      {report.data.availability === "not_ready" || !report.data.draft ? (
        <Alert className="mt-6">
          <FileCheck2Icon />
          <AlertTitle>报告尚未就绪</AlertTitle>
          <AlertDescription>{report.data.not_ready_reason}</AlertDescription>
        </Alert>
      ) : (
        <ReportWorkspace incidentId={incidentId} draft={report.data.draft} publications={report.data.publications} />
      )}
    </PageShell>
  )
}

function ReportWorkspace({
  incidentId,
  draft,
  publications,
}: {
  incidentId: string
  draft: IncidentReportDraft
  publications: Awaited<ReturnType<typeof getIncidentReport>>["publications"]
}) {
  const queryClient = useQueryClient()
  const [narrative, setNarrative] = useState<IncidentReportNarrative>(draft.narrative)
  const dirty = JSON.stringify(narrative) !== JSON.stringify(draft.narrative)
  const readonly = draft.status === "published"
  const refresh = () => queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "report"]})
  const save = useMutation({
    mutationFn: () => updateIncidentReport(incidentId, narrative),
    onSuccess: refresh,
  })
  const publish = useMutation({
    mutationFn: async () => {
      if (dirty) await updateIncidentReport(incidentId, narrative)
      return publishIncidentReport(incidentId)
    },
    onSuccess: refresh,
  })
  const facts = asRecord(draft.facts)
  const incident = asRecord(facts.incident)
  const history = asRecord(draft.decision_action_history)
  const changeRequests = records(history.change_requests)
  const phases = changeRequests.flatMap((changeRequest) => records(changeRequest.phases))
  const executions = phases.map((phase) => asRecord(phase.execution)).filter(hasFields)
  const reconciliations = executions.flatMap((execution) => records(execution.reconciliations))

  return (
    <div className="grid gap-8 py-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0">
        <form onSubmit={(event) => { event.preventDefault(); save.mutate() }}>
          <FieldSet disabled={readonly || save.isPending || publish.isPending}>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <FieldLegend>叙述内容</FieldLegend>
              {!readonly ? (
                <div className="flex gap-2">
                  <Button type="submit" variant="outline" disabled={!dirty}>
                    <SaveIcon data-icon="inline-start" />保存草稿
                  </Button>
                  <Button
                    type="button"
                    disabled={publish.isPending}
                    onClick={() => globalThis.confirm("发布后此版本不可编辑。确认发布？") && publish.mutate()}
                  >
                    <LockKeyholeIcon data-icon="inline-start" />发布版本
                  </Button>
                </div>
              ) : null}
            </div>
            <FieldGroup>
              {(Object.keys(narrativeLabels) as Array<keyof IncidentReportNarrative>).map((field) => (
                <Field key={field}>
                  <FieldLabel htmlFor={`report-${field}`}>{narrativeLabels[field]}</FieldLabel>
                  <Textarea
                    id={`report-${field}`}
                    rows={field === "follow_up" ? 5 : 4}
                    value={narrative[field]}
                    onChange={(event) => setNarrative((current) => ({...current, [field]: event.target.value}))}
                  />
                </Field>
              ))}
            </FieldGroup>
          </FieldSet>
          {save.isError || publish.isError ? (
            <Alert variant="destructive" className="mt-4"><AlertTitle>报告操作失败</AlertTitle></Alert>
          ) : null}
        </form>

        {publications.length ? (
          <section className="mt-10 border-t pt-6">
            <h2 className="text-base font-medium">已发布版本</h2>
            <div className="mt-3 flex flex-col gap-3">
              {publications.map((publication) => (
                <details key={publication.id} className="border-b pb-3">
                  <summary className="cursor-pointer py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    版本 {publication.version} · {formatTime(publication.published_at)}
                  </summary>
                  <dl className="grid gap-4 py-3 text-sm sm:grid-cols-2">
                    {(Object.keys(narrativeLabels) as Array<keyof IncidentReportNarrative>).map((field) => (
                      <div key={field}>
                        <dt className="text-xs text-muted-foreground">{narrativeLabels[field]}</dt>
                        <dd className="mt-1 whitespace-pre-wrap">{publication.narrative[field] || "未记录"}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              ))}
            </div>
          </section>
        ) : null}

        <ChangeGovernanceHistory changeRequests={changeRequests} />
      </div>

      <aside className="border-t pt-6 lg:border-t-0 lg:border-l lg:pl-6 lg:pt-0">
        <div className="flex items-center gap-2">
          <LockKeyholeIcon className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-medium">冻结事实</h2>
        </div>
        <dl className="mt-4 flex flex-col gap-3 text-sm">
          <Fact label="源修订" value={<MonoValue>{draft.source_revision}</MonoValue>} />
          <Fact label="解决时间" value={formatTime(draft.source_resolved_at)} />
          <Fact label="严重级别" value={text(incident.severity)} />
          <Fact label="集群 / 命名空间" value={`${text(incident.cluster_id)} / ${text(incident.namespace)}`} />
          <Fact label="调查轮次" value={draft.included_investigation_ids.length} />
          <Fact label="判断记录" value={array(history.judgments).length} />
          <Fact label="Recommendation guidance" value={array(history.recommended_actions).length} />
          <Fact label="Change Requests" value={changeRequests.length} />
          <Fact label="Phase Approvals" value={phases.filter((phase) => phase.approval).length} />
          <Fact label="Executions" value={executions.length} />
          <Fact label="Reconciliations" value={reconciliations.length} />
          <Fact label="证据引用" value={draft.evidence_references.length} />
        </dl>
        {draft.evidence_references.length ? (
          <>
            <Separator className="my-5" />
            <ul className="flex flex-col gap-2">
              {draft.evidence_references.map((reference) => <li key={reference}><MonoValue className="break-all">{reference}</MonoValue></li>)}
            </ul>
          </>
        ) : null}
      </aside>
    </div>
  )
}

export function ChangeGovernanceHistory({changeRequests}: {changeRequests: unknown[]}) {
  const requests = changeRequests.map(asRecord).filter(hasFields)
  if (!requests.length) return null
  return <section className="mt-10 border-t pt-6" aria-labelledby="change-history-heading">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 id="change-history-heading" className="text-base font-medium">Generic Change 治理历史</h2>
      <Badge variant="outline">{requests.length} Change Requests</Badge>
    </div>
    <div className="mt-4 divide-y border-y">
      {requests.map((changeRequest) => <details key={text(changeRequest.id)} className="py-1" open>
        <summary className="cursor-pointer py-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {text(changeRequest.desired_outcome)}
        </summary>
        <div className="pb-5">
          <dl className="grid gap-3 text-xs sm:grid-cols-3">
            <Fact label="Change Request" value={<MonoValue>{text(changeRequest.id)}</MonoValue>} />
            <Fact label="提交人" value={<MonoValue>{text(changeRequest.submitted_by)}</MonoValue>} />
            <Fact label="创建时间" value={time(changeRequest.created_at)} />
          </dl>
          {records(changeRequest.phases).map((phase) => <PhaseHistory key={text(phase.id)} phase={phase} />)}
          {records(changeRequest.events).length ? <div className="mt-4 border-t pt-3">
            <h4 className="text-xs font-medium text-muted-foreground">Transition events</h4>
            <ol className="mt-2 flex flex-col gap-1 text-xs">
              {records(changeRequest.events).map((event) => <li key={String(event.id)} className="flex flex-wrap justify-between gap-2">
                <MonoValue>{text(event.type)}</MonoValue><time>{time(event.created_at)}</time>
              </li>)}
            </ol>
          </div> : null}
        </div>
      </details>)}
    </div>
  </section>
}

function PhaseHistory({phase}: {phase: Record<string, unknown>}) {
  const revisions = records(phase.revisions)
  const approval = asRecord(phase.approval)
  const execution = asRecord(phase.execution)
  const steps = records(execution.steps)
  const reconciliations = records(execution.reconciliations)
  return <section className="mt-4 border-l-2 pl-4" aria-label={`Phase ${String(phase.sequence ?? "-")}`}>
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="text-sm font-medium">Phase {String(phase.sequence ?? "-")}</h3>
      <Badge variant="outline">{text(phase.status)}</Badge>
      <MonoValue>{text(phase.id)}</MonoValue>
    </div>
    {revisions.length ? <div className="mt-3">
      <h4 className="text-xs font-medium text-muted-foreground">Plan revisions</h4>
      <ol className="mt-2 divide-y border-y">
        {revisions.map((revision) => {
          const plan = asRecord(revision.plan)
          return <li key={text(revision.id)} className="py-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">Revision {String(revision.number ?? "-")}</Badge>
              <span>{text(revision.status)}</span>
              <span className="font-medium">{text(plan.summary)}</span>
            </div>
            {records(plan.changes).length ? <ul className="mt-2 flex flex-col gap-1 pl-3 text-muted-foreground">
              {records(plan.changes).map((change, index) => {
                const target = asRecord(change.target)
                return <li key={`${text(revision.id)}:${index}`}>
                  {text(change.operation)} {text(target.kind)} {text(target.namespace)}/{text(target.name)}
                </li>
              })}
            </ul> : null}
          </li>
        })}
      </ol>
    </div> : null}
    {hasFields(approval) ? <div className="mt-3">
      <h4 className="text-xs font-medium text-muted-foreground">Phase Approval</h4>
      <dl className="mt-2 grid gap-3 text-xs sm:grid-cols-3">
        <Fact label="Approver" value={<MonoValue>{text(approval.approver_id)}</MonoValue>} />
        <Fact label="审批原因" value={text(approval.reason)} />
        <Fact label="Rollback policy" value={text(approval.rollback_policy)} />
        <Fact label="审批时间" value={time(approval.approved_at)} />
      </dl>
    </div> : null}
    {hasFields(execution) ? <div className="mt-4 border-t pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-xs font-medium text-muted-foreground">Execution</h4>
        <Badge variant="secondary">{text(execution.status)}</Badge>
        <MonoValue>{text(execution.id)}</MonoValue>
      </div>
      <dl className="mt-2 grid gap-3 text-xs sm:grid-cols-3">
        <Fact label="执行人" value={<MonoValue>{text(execution.actor_id)}</MonoValue>} />
        <Fact label="执行原因" value={text(execution.reason)} />
        <Fact label="完成时间" value={time(execution.completed_at)} />
      </dl>
      {steps.length ? <ol className="mt-3 divide-y border-y">
        {steps.map((step) => <ExecutionStep key={text(step.id)} step={step} />)}
      </ol> : null}
      {reconciliations.length ? <div className="mt-4">
        <h5 className="text-xs font-medium text-muted-foreground">Reconciliation</h5>
        <ol className="mt-2 flex flex-col gap-3">
          {reconciliations.map((item) => <li key={text(item.id)} className="border-l-2 pl-3 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">{text(item.classification)}</Badge>
              <span>{text(item.state)}</span>
              <MonoValue>{text(item.evidence_sha256).slice(0, 12)}</MonoValue>
            </div>
            {item.accepted_by ? <p className="mt-1 text-muted-foreground">
              Accepted by <MonoValue>{text(item.accepted_by)}</MonoValue>: {text(item.acceptance_reason)}
            </p> : null}
          </li>)}
        </ol>
      </div> : null}
    </div> : null}
  </section>
}

function ExecutionStep({step}: {step: Record<string, unknown>}) {
  const change = asRecord(step.change)
  const target = asRecord(change.target)
  const result = asRecord(step.result)
  const direction = text(step.direction)
  return <li className="grid gap-2 py-3 text-xs sm:grid-cols-[100px_minmax(0,1fr)_120px]">
    <div><Badge variant={direction === "rollback" ? "warning" : "outline"}>{direction}</Badge></div>
    <div className="min-w-0">
      <div className="font-medium">{text(change.operation)} {text(target.kind)} {text(target.namespace)}/{text(target.name)}</div>
      <MonoValue className="mt-1 block break-all">{text(step.command_id)}</MonoValue>
      {result.error_code ? <p className="mt-1 text-destructive">{text(result.error_code)}: {text(result.error_message)}</p> : null}
    </div>
    <div className="sm:text-right"><Badge variant="secondary">{text(step.status)}</Badge></div>
  </li>
}

function PageShell({children}: {children: React.ReactNode}) {
  return <main className="mx-auto max-w-[1200px] px-4 py-7 lg:px-6">{children}</main>
}

function PageStatus({children}: {children: React.ReactNode}) {
  return <div className="grid min-h-64 place-items-center text-sm text-muted-foreground" role="status">{children}</div>
}

function Fact({label, value}: {label: string; value: React.ReactNode}) {
  return <div><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-0.5 break-words">{value}</dd></div>
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function records(value: unknown): Record<string, unknown>[] {
  return array(value).map(asRecord).filter(hasFields)
}

function hasFields(value: Record<string, unknown>): boolean {
  return Object.keys(value).length > 0
}

function text(value: unknown): string {
  return typeof value === "string" && value ? value : "未记录"
}

function time(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? formatTime(value) : "未记录"
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"}).format(new Date(value * 1000))
}
