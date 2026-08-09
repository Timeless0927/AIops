import {
  EyeIcon,
} from "lucide-react"

import type { Incident } from "@/incidents/incident-client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type {
  EvidenceRelation,
  EvidenceStatus,
  IncidentSeverity,
  IncidentState,
} from "@/prototype/data"

export const incidentLifecycleLabels = {firing: "告警中", stabilizing: "稳定观察中", resolved: "告警已恢复", reopened: "重新打开"} as const

type DiagnosisStatus = Pick<Incident, "diagnosis_outcome" | "evidence_gate_status">

export function diagnosisStatusLabel(
  outcome: Incident["diagnosis_outcome"],
  gate: Incident["evidence_gate_status"],
) {
  if (outcome === "failed") return "诊断失败"
  if (outcome === "needs_human") return "诊断需人工处理"
  if (outcome && (gate !== "complete" || outcome === "partial")) return "诊断证据不足"
  return outcome ? "诊断已完成" : null
}

export function DiagnosisStatusBadge({diagnosis_outcome: outcome, evidence_gate_status: gate}: DiagnosisStatus) {
  const label = diagnosisStatusLabel(outcome, gate)
  if (!label) return null
  const variant = label === "诊断失败" ? "destructive" : label === "诊断已完成" ? "secondary" : "warning"
  return <Badge variant={variant}>{label}</Badge>
}

const severityLabels: Record<IncidentSeverity, string> = {
  critical: "严重",
  high: "高",
  medium: "中",
}

const stateLabels: Record<IncidentState, string> = {
  investigating: "调查中",
  stabilizing: "稳定观察",
  waiting: "等待证据",
  resolved: "已解决",
}

const relationLabels: Record<EvidenceRelation, string> = {
  supports: "支持判断",
  challenges: "反证",
  context: "上下文",
}

const statusLabels: Record<EvidenceStatus, string> = {
  verified: "已验证",
  partial: "部分证据",
  missing: "缺失",
}

export function SeverityBadge({ severity }: { severity: IncidentSeverity }) {
  return (
    <Badge variant={severity === "critical" ? "destructive" : severity === "high" ? "warning" : "outline"}>
      {severityLabels[severity]}
    </Badge>
  )
}

export function IncidentStateBadge({ state }: { state: IncidentState }) {
  const variant =
    state === "resolved"
      ? "positive"
      : state === "waiting"
        ? "warning"
        : state === "investigating"
          ? "evidence"
          : "outline"

  return <Badge variant={variant}>{stateLabels[state]}</Badge>
}

export function EvidenceRelationBadge({ relation }: { relation: EvidenceRelation }) {
  const variant =
    relation === "supports" ? "evidence" : relation === "challenges" ? "warning" : "outline"
  return <Badge variant={variant}>{relationLabels[relation]}</Badge>
}

export function EvidenceStatusBadge({ status }: { status: EvidenceStatus }) {
  const variant = status === "verified" ? "positive" : status === "partial" ? "warning" : "destructive"
  return <Badge variant={variant}>{statusLabels[status]}</Badge>
}

export function EvidenceViewButton({
  evidenceId,
  onOpen,
}: {
  evidenceId: string
  onOpen: (id: string) => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="查看证据"
            onClick={() => onOpen(evidenceId)}
          />
        }
      >
        <EyeIcon />
      </TooltipTrigger>
      <TooltipContent>查看证据</TooltipContent>
    </Tooltip>
  )
}

export function MonoValue({ className, children }: { className?: string; children: React.ReactNode }) {
  return <span className={cn("font-mono text-xs tabular-nums", className)}>{children}</span>
}
