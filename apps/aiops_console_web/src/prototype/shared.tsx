import type { LucideIcon } from "lucide-react"
import {
  ActivityIcon,
  ArrowLeftIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  Clock3Icon,
  EyeIcon,
  RadioIcon,
  ServerIcon,
  ShieldCheckIcon,
  UserRoundIcon,
} from "lucide-react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type {
  EvidenceFixture,
  EvidenceRelation,
  EvidenceStatus,
  IncidentSeverity,
  IncidentState,
} from "@/prototype/data"

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

export function ConsoleHeader({ showBack = false }: { showBack?: boolean }) {
  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex h-13 max-w-[1600px] items-center gap-3 px-4 lg:px-6">
        {showBack ? (
          <Link
            to="/prototype/incidents"
            className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
            aria-label="返回事件列表"
          >
            <ArrowLeftIcon />
          </Link>
        ) : null}

        <Link
          to="/prototype/incidents"
          className="flex min-w-0 items-center gap-2.5 text-sm font-medium"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-background">
            <ActivityIcon className="size-4" />
          </span>
          <span className="hidden sm:inline">AIOps Control Plane</span>
          <span className="font-mono text-[11px] text-muted-foreground sm:hidden">AIOps</span>
        </Link>

        <nav aria-label="主导航" className="ml-3 hidden items-center sm:flex">
          <Link
            to="/prototype/incidents"
            className="border-b-2 border-foreground px-3 py-4 text-sm font-medium"
          >
            事件
          </Link>
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <Badge variant="evidence" className="hidden sm:inline-flex">
            <RadioIcon data-icon="inline-start" />
            实时连接
          </Badge>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button variant="ghost" size="icon-sm" aria-label="当前用户" />
              }
            >
              <UserRoundIcon />
            </TooltipTrigger>
            <TooltipContent>王晨 · 值班 SRE</TooltipContent>
          </Tooltip>
        </div>
      </div>
    </header>
  )
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

function FactRow({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="grid grid-cols-[20px_92px_minmax(0,1fr)] items-start gap-2 py-2 text-sm">
      <Icon className="mt-0.5 size-4 text-muted-foreground" />
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words font-medium">{value}</dd>
    </div>
  )
}

export function EvidenceDetailSheet({
  item,
  onClose,
}: {
  item: EvidenceFixture | null
  onClose: () => void
}) {
  return (
    <Sheet open={Boolean(item)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-[460px]">
        {item ? (
          <>
            <SheetHeader className="border-b">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <EvidenceRelationBadge relation={item.relation} />
                <EvidenceStatusBadge status={item.status} />
              </div>
              <SheetTitle>{item.title}</SheetTitle>
              <SheetDescription>{item.summary}</SheetDescription>
            </SheetHeader>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6">
              <dl className="divide-y">
                <FactRow icon={Clock3Icon} label="时间" value={`${item.time} · ${item.phase}`} />
                <FactRow icon={ServerIcon} label="来源" value={item.source} />
                <FactRow icon={ShieldCheckIcon} label="范围" value={item.scope} />
                <FactRow icon={ActivityIcon} label="查询目的" value={item.query} />
              </dl>

              <section className="mt-5" aria-labelledby="evidence-impact-title">
                <h3 id="evidence-impact-title" className="text-sm font-medium">
                  对当前判断的影响
                </h3>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">{item.impact}</p>
              </section>

              <section className="mt-5" aria-labelledby="evidence-samples-title">
                <h3 id="evidence-samples-title" className="text-sm font-medium">
                  结构化样本
                </h3>
                <div className="mt-2 divide-y rounded-md border bg-surface px-3">
                  {item.samples.map((sample) => (
                    <div key={sample.label} className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 py-2.5 text-sm">
                      <span className="text-muted-foreground">{sample.label}</span>
                      <span className="break-words font-mono text-xs leading-5">{sample.value}</span>
                    </div>
                  ))}
                </div>
              </section>

              {item.status !== "verified" ? (
                <div className="mt-5 flex gap-2 rounded-md border border-warning/30 bg-warning/8 p-3 text-sm text-warning-foreground">
                  <CircleAlertIcon className="mt-0.5 size-4 shrink-0" />
                  <p>该步骤尚不能作为完整执行依据，需先补齐缺失范围。</p>
                </div>
              ) : (
                <div className="mt-5 flex gap-2 rounded-md border border-positive/30 bg-positive/8 p-3 text-sm text-positive-foreground">
                  <CheckCircle2Icon className="mt-0.5 size-4 shrink-0" />
                  <p>该证据已完成范围与引用完整性校验。</p>
                </div>
              )}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}

export function MonoValue({ className, children }: { className?: string; children: React.ReactNode }) {
  return <span className={cn("font-mono text-xs tabular-nums", className)}>{children}</span>
}
