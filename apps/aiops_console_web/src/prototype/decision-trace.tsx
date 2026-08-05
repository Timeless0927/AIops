import type { InvestigationEvent, Workbench } from "@/api/client"
import { Badge } from "@/components/ui/badge"

type Relation = "supports" | "refutes" | "uncertain"
type SkillVersion = {id: string; name: string; version: number}
type Candidate = {
  cause: string
  confidence?: number
  evidence_relations: {evidence_ref: string; relation: Relation}[]
  unknowns: string[]
  next_checks: string[]
}
type ToolActivity = {
  tool: string
  purpose: string
  authorized_scope: Record<string, string>
  status: string
  duration_ms: number | null
  summary: string
  missing_reason?: string
  status_reason?: string
  evidence_references: string[]
  candidate_impacts: {cause: string; relation: Relation}[]
  truncation: {truncated: boolean; limit_bytes: number; reason?: string}
  redaction: {applied: boolean; note: string}
  continuation_reason?: string
  stopping_reason?: string
  skill_versions: SkillVersion[]
}
type Trace = {
  goal: string
  skill_versions: SkillVersion[]
  tool_activity: ToolActivity[]
  candidates: Candidate[]
  completion: {
    status: string
    issues: string[]
    repair_attempts: number
    stopping_reason: string
    remaining_evidence_steps: number
  }
}

const relationLabels: Record<Relation, string> = {
  supports: "支持",
  refutes: "反驳",
  uncertain: "关系待确认",
}
const toolStatusLabels: Record<string, string> = {
  running: "进行中",
  succeeded: "已取得",
  partial: "部分取得",
  failed: "失败",
  skipped: "已跳过",
  needs_human: "需要人工处理",
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function strings(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
}

function skillVersions(value: unknown): SkillVersion[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const skill = object(item)
    return skill && typeof skill.id === "string" && typeof skill.name === "string"
      && typeof skill.version === "number"
      ? [{id: skill.id, name: skill.name, version: skill.version}]
      : []
  })
}

function relation(value: unknown): Relation | null {
  return value === "supports" || value === "refutes" || value === "uncertain" ? value : null
}

function candidates(value: unknown): Candidate[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const candidate = object(item)
    if (!candidate || typeof candidate.cause !== "string") return []
    return [{
      cause: candidate.cause,
      ...(typeof candidate.confidence === "number" ? {confidence: candidate.confidence} : {}),
      evidence_relations: Array.isArray(candidate.evidence_relations)
        ? candidate.evidence_relations.flatMap((item) => {
          const relationship = object(item)
          const kind = relation(relationship?.relation)
          return relationship && typeof relationship.evidence_ref === "string" && kind
            ? [{evidence_ref: relationship.evidence_ref, relation: kind}]
            : []
        })
        : [],
      unknowns: strings(candidate.unknowns),
      next_checks: strings(candidate.next_checks),
    }]
  })
}

function activities(value: unknown): ToolActivity[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const activity = object(item)
    const scope = object(activity?.authorized_scope)
    const truncation = object(activity?.truncation)
    const redaction = object(activity?.redaction)
    if (!activity || typeof activity.tool !== "string" || typeof activity.purpose !== "string") return []
    return [{
      tool: activity.tool,
      purpose: activity.purpose,
      authorized_scope: Object.fromEntries(
        Object.entries(scope ?? {}).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
      ),
      status: typeof activity.status === "string" ? activity.status : "failed",
      duration_ms: typeof activity.duration_ms === "number" ? activity.duration_ms : null,
      summary: typeof activity.summary === "string" ? activity.summary : "工具未返回摘要",
      ...(typeof activity.missing_reason === "string" ? {missing_reason: activity.missing_reason} : {}),
      ...(typeof activity.status_reason === "string" ? {status_reason: activity.status_reason} : {}),
      evidence_references: strings(activity.evidence_references),
      candidate_impacts: Array.isArray(activity.candidate_impacts)
        ? activity.candidate_impacts.flatMap((item) => {
          const impact = object(item)
          const kind = relation(impact?.relation)
          return impact && typeof impact.cause === "string" && kind
            ? [{cause: impact.cause, relation: kind}]
            : []
        })
        : [],
      truncation: {
        truncated: truncation?.truncated === true,
        limit_bytes: typeof truncation?.limit_bytes === "number" ? truncation.limit_bytes : 0,
        ...(typeof truncation?.reason === "string" ? {reason: truncation.reason} : {}),
      },
      redaction: {
        applied: redaction?.applied === true,
        note: typeof redaction?.note === "string" ? redaction.note : "未报告脱敏",
      },
      ...(typeof activity.continuation_reason === "string" ? {continuation_reason: activity.continuation_reason} : {}),
      ...(typeof activity.stopping_reason === "string" ? {stopping_reason: activity.stopping_reason} : {}),
      skill_versions: skillVersions(activity.skill_versions),
    }]
  })
}

export function decisionTraceFromEvents(events: InvestigationEvent[]): Trace | null {
  const event = [...events].reverse().find((item) => item.type === "diagnosis.output")
  const trace = object(event?.payload.decision_trace)
  const completion = object(trace?.completion)
  if (!trace || !completion || typeof trace.goal !== "string") return null
  return {
    goal: trace.goal,
    skill_versions: skillVersions(trace.skill_versions),
    tool_activity: activities(trace.tool_activity),
    candidates: candidates(trace.candidates),
    completion: {
      status: typeof completion.status === "string" ? completion.status : "unknown",
      issues: strings(completion.issues),
      repair_attempts: typeof completion.repair_attempts === "number" ? completion.repair_attempts : 0,
      stopping_reason: typeof completion.stopping_reason === "string" ? completion.stopping_reason : "unknown",
      remaining_evidence_steps: typeof completion.remaining_evidence_steps === "number"
        ? completion.remaining_evidence_steps
        : 0,
    },
  }
}

function ToolActivityDetails({activity}: {activity: ToolActivity}) {
  const scope = Object.values(activity.authorized_scope).join(" / ") || "未报告范围"
  return (
    <details className="rounded-md border bg-surface px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium">
        {activity.purpose} · {toolStatusLabels[activity.status] ?? activity.status}
      </summary>
      <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
        <div><dt className="text-xs text-muted-foreground">工具 / 范围</dt><dd>{activity.tool} · {scope}</dd></div>
        <div><dt className="text-xs text-muted-foreground">耗时</dt><dd>{activity.duration_ms === null ? "时长未记录" : `${activity.duration_ms} ms`}</dd></div>
        <div className="sm:col-span-2"><dt className="text-xs text-muted-foreground">结果</dt><dd>{activity.summary}</dd></div>
        {activity.missing_reason ? <div className="sm:col-span-2"><dt className="text-xs text-muted-foreground">缺失原因</dt><dd className="text-destructive">{activity.missing_reason}</dd></div> : null}
        {activity.status_reason && activity.status_reason !== activity.missing_reason ? <div className="sm:col-span-2"><dt className="text-xs text-muted-foreground">状态原因</dt><dd>{activity.status_reason}</dd></div> : null}
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        {activity.truncation.truncated ? <Badge variant="outline">已截断</Badge> : null}
        {activity.redaction.applied ? <Badge variant="outline">已脱敏</Badge> : null}
        {activity.evidence_references.map((reference) => <Badge key={reference} variant="secondary">{reference}</Badge>)}
        {activity.skill_versions.map((skill) => <Badge key={skill.id} variant="outline">{skill.name} v{skill.version}</Badge>)}
      </div>
      {activity.truncation.truncated ? <p className="mt-2 text-xs text-muted-foreground">{activity.truncation.reason}</p> : null}
      {activity.redaction.applied ? <p className="mt-1 text-xs text-muted-foreground">{activity.redaction.note}</p> : null}
      {activity.candidate_impacts.length ? <ul className="mt-3 space-y-1 text-sm text-muted-foreground">
        {activity.candidate_impacts.map((impact) => <li key={`${impact.cause}:${impact.relation}`}>{relationLabels[impact.relation]} {impact.cause}</li>)}
      </ul> : null}
      <p className="mt-3 text-xs text-muted-foreground">
        {activity.stopping_reason ? `停止原因：${activity.stopping_reason}` : `继续原因：${activity.continuation_reason}`}
      </p>
    </details>
  )
}

export function DecisionTrace({
  events,
  judgment,
}: {
  events: InvestigationEvent[]
  judgment: Workbench["judgment"]
}) {
  const trace = decisionTraceFromEvents(events)
  if (!trace) return <section className="border-b p-4"><h2 className="font-semibold">Decision Trace</h2><p className="mt-2 text-sm text-muted-foreground">尚无 Decision Trace</p></section>
  const gate = judgment?.evidence_gate_status === "complete" && judgment.valid ? "完整" : "不完整"
  return (
    <section className="border-b" aria-labelledby="decision-trace-title">
      <header className="flex flex-wrap items-center gap-2 border-b p-4">
        <div>
          <h2 id="decision-trace-title" className="font-semibold">Decision Trace</h2>
          <p className="mt-1 text-sm text-muted-foreground">{trace.goal}</p>
          {trace.skill_versions.length ? <div className="mt-2 flex flex-wrap gap-1">{trace.skill_versions.map((skill) => <Badge key={skill.id} variant="outline">{skill.name} v{skill.version}</Badge>)}</div> : null}
        </div>
        <Badge className="ml-auto" variant={gate === "完整" ? "default" : "outline"}>Evidence Gate：{gate}</Badge>
      </header>
      <div className="grid gap-4 p-4 lg:grid-cols-2">
        <div>
          <h3 className="text-sm font-medium">Tool Activity</h3>
          <div className="mt-2 space-y-2">
            {trace.tool_activity.map((activity, index) => <ToolActivityDetails key={`${activity.tool}:${index}`} activity={activity} />)}
          </div>
        </div>
        <div>
          <h3 className="text-sm font-medium">候选原因</h3>
          <div className="mt-2 space-y-2">
            {trace.candidates.map((candidate) => <article key={candidate.cause} className="rounded-md border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-sm font-medium">{candidate.cause}</h4>
                {candidate.confidence !== undefined ? <Badge variant="outline">{Math.round(candidate.confidence * 100)}%（仅供参考）</Badge> : null}
              </div>
              <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                {candidate.evidence_relations.map((item) => <li key={`${item.evidence_ref}:${item.relation}`}>{relationLabels[item.relation]} {item.evidence_ref}</li>)}
                {candidate.unknowns.map((item) => <li key={item}>未知：{item}</li>)}
                {candidate.next_checks.map((item) => <li key={item}>下一步：{item}</li>)}
              </ul>
            </article>)}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">停止原因：{trace.completion.stopping_reason}</p>
        </div>
      </div>
    </section>
  )
}
