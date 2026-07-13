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
          <Fact label="建议动作" value={array(history.recommended_actions).length} />
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

function text(value: unknown): string {
  return typeof value === "string" && value ? value : "未记录"
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"}).format(new Date(value * 1000))
}
