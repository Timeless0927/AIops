import { useQuery } from "@tanstack/react-query"
import { ActivityIcon, SearchIcon } from "lucide-react"
import { Link, useSearchParams } from "react-router"

import { listIncidents, type Incident } from "@/incidents/incident-client"
import { PageHeader } from "@/components/page-header"
import { ListSkeleton } from "@/components/page-skeleton"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { readIncidentListState, updateIncidentListSearch, type IncidentFilter } from "@/prototype/incident-list-state"
import { DiagnosisStatusBadge, incidentLifecycleLabels, MonoValue } from "@/prototype/shared"
import { cn, formatRelativeTime } from "@/lib/utils"

const bindingLabel = {bound: "已绑定", unbound: "未绑定"}
const filterLabel: Record<IncidentFilter, string> = {active: "进行中", waiting: "等待观察", resolved: "已恢复"}
const severityLabel: Record<Incident["severity"], string> = {critical: "严重", high: "高", medium: "中", low: "低"}

const severityRank: Record<Incident["severity"], number> = {critical: 0, high: 1, medium: 2, low: 3}
const severityDot: Record<Incident["severity"], string> = {
  critical: "bg-destructive",
  high: "bg-warning",
  medium: "bg-evidence",
  low: "bg-muted-foreground/40",
}

function matchesFilter(incident: Incident, filter: IncidentFilter) {
  if (filter === "active") return incident.lifecycle_state === "firing" || incident.lifecycle_state === "reopened"
  if (filter === "waiting") return incident.lifecycle_state === "stabilizing"
  return incident.lifecycle_state === "resolved"
}

function sortIncidents(list: Incident[]) {
  return [...list].sort((a, b) =>
    severityRank[a.severity] - severityRank[b.severity] || b.updated_at - a.updated_at,
  )
}

function StatCard({label, value, description, tone}: {label: string; value: number; description: string; tone?: "destructive" | "warning"}) {
  return (
    <Card size="sm">
      <CardContent>
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={cn(
          "mt-1 font-mono text-2xl font-semibold tabular-nums",
          tone === "destructive" && value > 0 ? "text-destructive" : tone === "warning" && value > 0 ? "text-warning-foreground" : undefined,
        )}>{value}</p>
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      </CardContent>
    </Card>
  )
}

export function IncidentsPrototypePage() {
  const incidents = useQuery({queryKey: ["incidents"], queryFn: listIncidents})
  const [params, setParams] = useSearchParams()
  const {filter, query} = readIncidentListState(params)

  if (incidents.isPending) return <ListSkeleton />
  if (incidents.isError) {
    return (
      <main className="mx-auto max-w-[1500px] px-4 py-6 lg:px-6 lg:py-8">
        <Empty className="min-h-72 border">
          <EmptyHeader>
            <EmptyTitle>无法加载事件</EmptyTitle>
            <EmptyDescription>请刷新页面重试。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </main>
    )
  }

  const data = incidents.data
  const activeCount = data.filter((item) => matchesFilter(item, "active")).length
  const criticalCount = data.filter((item) => item.severity === "critical" && matchesFilter(item, "active")).length
  const waitingCount = data.filter((item) => matchesFilter(item, "waiting")).length
  const needle = query.trim().toLocaleLowerCase("zh-CN")
  const visible = sortIncidents(data.filter((item) =>
    matchesFilter(item, filter)
    && (!needle || item.title.toLocaleLowerCase("zh-CN").includes(needle) || item.id.toLocaleLowerCase("zh-CN").includes(needle)),
  ))

  return (
    <main className="mx-auto max-w-[1500px] px-4 py-6 lg:px-6 lg:py-8">
      <PageHeader title="事件" description="按严重度排序的告警事件与调查入口" />

      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <StatCard label="进行中" value={activeCount} description="告警中或重新打开" tone={activeCount ? "destructive" : undefined} />
        <StatCard label="严重事件" value={criticalCount} description="进行中且严重度为严重" tone={criticalCount ? "warning" : undefined} />
        <StatCard label="等待观察" value={waitingCount} description="已稳定，等待恢复确认" />
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        <ToggleGroup
          value={[filter]}
          onValueChange={(value) => {
            const next = value[0] as IncidentFilter | undefined
            if (next) setParams(updateIncidentListSearch(params, "status", next), {replace: true})
          }}
          aria-label="按状态筛选"
        >
          {(["active", "waiting", "resolved"] as const).map((value) => (
            <ToggleGroupItem key={value} value={value} aria-label={filterLabel[value]}>{filterLabel[value]}</ToggleGroupItem>
          ))}
        </ToggleGroup>
        <div className="relative min-w-52 flex-1 sm:max-w-xs">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(event) => setParams(updateIncidentListSearch(params, "q", event.target.value), {replace: true})}
            aria-label="搜索事件"
            placeholder="搜索标题或事件 ID"
            className="pl-8"
          />
        </div>
      </div>

      {data.length === 0 ? (
        <Empty className="mt-6 min-h-72 border">
          <EmptyHeader>
            <EmptyMedia variant="icon"><ActivityIcon /></EmptyMedia>
            <EmptyTitle>暂无事件</EmptyTitle>
            <EmptyDescription>收到有效告警后，事件会显示在这里。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : visible.length === 0 ? (
        <Empty className="mt-6 min-h-72 border">
          <EmptyHeader>
            <EmptyMedia variant="icon"><SearchIcon /></EmptyMedia>
            <EmptyTitle>没有匹配的事件</EmptyTitle>
            <EmptyDescription>调整状态筛选或搜索关键词后重试。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ul className="mt-4 grid gap-2" aria-label="事件列表">
          {visible.map((incident, index) => (
            <li
              key={incident.id}
              className="animate-in fade-in slide-in-from-bottom-1 rounded-lg fill-mode-both motion-reduce:animate-none"
              style={{animationDelay: `${Math.min(index, 10) * 30}ms`}}
            >
              <Link
                to={`/incidents/${incident.id}`}
                className="flex gap-3 rounded-lg border bg-card/50 px-3 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring sm:items-center"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "mt-1.5 size-2 shrink-0 rounded-full sm:mt-0",
                    severityDot[incident.severity],
                    incident.severity === "critical" && incident.lifecycle_state === "firing" ? "animate-pulse motion-reduce:animate-none" : undefined,
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-medium">{incident.title}</span>
                    <span className="text-xs text-muted-foreground">{severityLabel[incident.severity]}</span>
                  </span>
                  <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <MonoValue>{incident.id}</MonoValue>
                    <span>{incident.cluster_name} / {incident.namespace}</span>
                    <span>{incident.signal_count} 个信号</span>
                    <time dateTime={new Date(incident.updated_at * 1000).toISOString()}>{formatRelativeTime(incident.updated_at)}</time>
                  </span>
                </span>
                <span className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                  <Badge variant={incident.lifecycle_state === "resolved" ? "secondary" : incident.lifecycle_state === "stabilizing" ? "warning" : "destructive"}>
                    {incidentLifecycleLabels[incident.lifecycle_state]}
                  </Badge>
                  <DiagnosisStatusBadge {...incident} />
                  <Badge variant="outline" className="hidden sm:inline-flex">{bindingLabel[incident.binding_status]}</Badge>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}
