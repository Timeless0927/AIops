import {
  EyeIcon,
} from "lucide-react"

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

export const incidentLifecycleLabels = {firing: "告警中", stabilizing: "稳定观察中", resolved: "已解决", reopened: "重新打开"} as const

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
