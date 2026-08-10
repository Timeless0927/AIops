import { useQuery } from "@tanstack/react-query"
import { CircleDotIcon, GitPullRequestCreateIcon } from "lucide-react"
import { Link, useLocation, useParams, useSearchParams } from "react-router"

import {
  getChangeCenterDetail,
  listChangeCenter,
  type ChangeRequest,
  type ChangeCenterDetail,
  type ChangeCenterSummary,
} from "@/changes/change-client"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ChangeRequestsSection } from "@/changes/change-requests-section"
import { statusLabel } from "@/changes/change-request-governance"
import { ListSkeleton } from "@/components/page-skeleton"
import { PageHeader } from "@/components/page-header"
import { MonoValue } from "@/prototype/shared"
import { formatRelativeTime } from "@/lib/utils"

const statusFilters = ["all", "attention", "active", "paused", "terminal"] as const
const environments = ["all", "prod", "staging", "dev", "test"] as const
const activeStatuses = new Set([
  "planning", "needs_input", "validating", "awaiting_approval", "approved", "executing",
])
const pausedStatuses = new Set([
  "unknown_outcome", "effect_observed", "cancel_requested", "rolling_back",
  "secure_input_unavailable",
])
const terminalStatuses = new Set([
  "succeeded", "failed", "expired", "cancelled", "rolled_back", "rollback_failed",
])

export type ChangeCenterFilters = {
  status: typeof statusFilters[number]
  environment: typeof environments[number]
}

const statusFilterLabel: Record<ChangeCenterFilters["status"], string> = {
  all: "全部状态",
  attention: "待我处理",
  active: "进行中",
  paused: "已暂停",
  terminal: "已结束",
}

const environmentLabel: Record<ChangeCenterFilters["environment"], string> = {
  all: "全部环境",
  prod: "Production",
  staging: "Staging",
  dev: "Development",
  test: "Test",
}

const attentionLabel = {
  input: "需要输入",
  retry: "可重试",
  approval: "等待审批",
  execution: "需要操作",
  reconciliation: "等待确认",
}

const eventLabel: Record<string, string> = {
  "change_request.created": "已创建",
  "change_request.input_received": "已补充输入",
  "change_request.planning_retried": "已重试规划",
  "change_request.needs_input": "规划需要输入",
  "change_request.validation_started": "验证已开始",
  "change_request.validation_succeeded": "验证已通过",
  "change_request.validation_failed": "验证失败",
  "change_request.phase_approved": "Phase 已审批",
  "change_request.phase_expired": "Phase 已过期",
  "change_request.execution_queued": "执行已排队",
  "change_request.execution_started": "执行已开始",
  "change_request.execution_finished": "执行已结束",
  "change_request.execution_outcome_unknown": "执行结果未知",
  "change_request.execution_cancel_requested": "已请求取消执行",
  "change_request.execution_cancelled": "执行已取消",
  "change_request.rollback_started": "回滚已开始",
  "change_request.rollback_finished": "回滚已结束",
  "change_request.reconciliation_observed": "已观察 Reconciliation",
  "change_request.reconciliation_accepted": "已接受 Reconciliation",
}

export function changeCenterFilters(params: URLSearchParams): ChangeCenterFilters {
  const status = params.get("status")
  const environment = params.get("environment")
  return {
    status: statusFilters.includes(status as ChangeCenterFilters["status"])
      ? status as ChangeCenterFilters["status"] : "all",
    environment: environments.includes(environment as ChangeCenterFilters["environment"])
      ? environment as ChangeCenterFilters["environment"] : "all",
  }
}

export function filterChangeCenter(
  changeRequests: ChangeCenterSummary[],
  filters: ChangeCenterFilters,
) {
  return changeRequests.filter((item) => {
    if (filters.environment !== "all" && item.environment !== filters.environment) return false
    if (filters.status === "attention") return item.attention !== null
    if (filters.status === "active") return activeStatuses.has(item.status)
    if (filters.status === "paused") return pausedStatuses.has(item.status)
    if (filters.status === "terminal") return terminalStatuses.has(item.status)
    return true
  })
}

function filterSearch(filters: ChangeCenterFilters) {
  const params = new URLSearchParams()
  if (filters.status !== "all") params.set("status", filters.status)
  if (filters.environment !== "all") params.set("environment", filters.environment)
  const search = params.toString()
  return search ? `?${search}` : ""
}

export function ChangeCenterListView({
  changeRequests,
  pendingCount,
  filters,
  onFiltersChange,
}: {
  changeRequests: ChangeCenterSummary[]
  pendingCount: number
  filters: ChangeCenterFilters
  onFiltersChange?: (filters: ChangeCenterFilters) => void
}) {
  const filtered = filterChangeCenter(changeRequests, filters)
  const search = filterSearch(filters)
  return <main className="mx-auto w-full max-w-[1600px] min-w-0 px-3 py-5 sm:px-4 lg:px-6">
    <PageHeader title="变更" description="跨事件 Change Request">
      <Badge variant={pendingCount ? "warning" : "secondary"}>待处理 {pendingCount}</Badge>
    </PageHeader>

    <section aria-label="变更筛选" className="flex min-w-0 flex-wrap gap-2 py-3">
      <Select
        value={filters.status}
        onValueChange={(status) => onFiltersChange?.({
          ...filters, status: status as ChangeCenterFilters["status"],
        })}
      >
        <SelectTrigger aria-label="按状态筛选" className="min-w-32">
          <SelectValue>{statusFilterLabel[filters.status]}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {statusFilters.map((status) => <SelectItem key={status} value={status}>
              {statusFilterLabel[status]}
            </SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
      <Select
        value={filters.environment}
        onValueChange={(environment) => onFiltersChange?.({
          ...filters, environment: environment as ChangeCenterFilters["environment"],
        })}
      >
        <SelectTrigger aria-label="按环境筛选" className="min-w-36">
          <SelectValue>{environmentLabel[filters.environment]}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {environments.map((environment) => <SelectItem key={environment} value={environment}>
              {environmentLabel[environment]}
            </SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
    </section>

    {filtered.length ? <ul className="grid gap-2" aria-label="Change Request 列表">
      {filtered.map((item) => <li key={item.id} className="animate-in fade-in slide-in-from-bottom-1 fill-mode-both motion-reduce:animate-none">
        <Link
          to={{pathname: `/changes/${item.id}`, search}}
          className="grid min-w-0 gap-2 rounded-lg border bg-card/50 px-3 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[minmax(0,1.4fr)_minmax(12rem,0.8fr)_auto] sm:items-center"
        >
          <div className="min-w-0">
            <div className="break-words text-sm font-medium">{item.desired_outcome}</div>
            <div className="mt-1 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span className="break-words">{item.incident.title}</span>
              <span>{item.environment}</span>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap gap-2">
            <Badge variant="outline">{statusLabel[item.status]}</Badge>
            {item.attention ? <Badge variant="warning">{attentionLabel[item.attention]}</Badge> : null}
          </div>
          <time className="text-xs text-muted-foreground" dateTime={new Date(item.updated_at * 1000).toISOString()}>
            {formatRelativeTime(item.updated_at)}
          </time>
        </Link>
      </li>)}
    </ul> : <Empty className="min-h-64 border rounded-lg">
      <EmptyHeader>
        <EmptyMedia variant="icon"><GitPullRequestCreateIcon /></EmptyMedia>
        <EmptyTitle>没有匹配的变更</EmptyTitle>
        <EmptyDescription>当前筛选范围内没有 Change Request。</EmptyDescription>
      </EmptyHeader>
    </Empty>}
  </main>
}

export function ChangeCenterDetailView({detail}: {detail: ChangeCenterDetail}) {
  const {search} = useLocation()
  return <main className="mx-auto w-full max-w-[1600px] min-w-0 px-3 py-5 sm:px-4 lg:px-6">
    <header className="flex min-w-0 flex-wrap items-start gap-3 border-b pb-4">
      <div className="min-w-0 flex-1">
        <Link to={{pathname: "/changes", search}} className="text-xs text-muted-foreground hover:text-foreground">
          变更
        </Link>
        <h1 className="mt-1 break-words text-xl font-semibold">{detail.change_request.desired_outcome}</h1>
        <Link
          to={`/incidents/${detail.incident.id}`}
          className="mt-1 inline-flex min-w-0 items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <CircleDotIcon />
          <span className="truncate">{detail.incident.title}</span>
        </Link>
      </div>
      <div className="flex flex-wrap gap-2">
        <Badge variant="outline">{detail.environment}</Badge>
        <Badge variant="secondary">{statusLabel[detail.change_request.status]}</Badge>
      </div>
    </header>
    <section className="border-b py-4" aria-labelledby="change-evidence-title">
      <h2 id="change-evidence-title" className="text-sm font-semibold">Evidence references</h2>
      {detail.evidence_references.length ? <ul className="mt-2 grid gap-1 text-xs">
        {detail.evidence_references.map((reference) => <li key={reference} className="min-w-0 break-all">
          <MonoValue>{reference}</MonoValue>
        </li>)}
      </ul> : <p className="mt-2 text-xs text-muted-foreground">无 Evidence reference</p>}
    </section>
    <Card className="mt-2 gap-0 py-0">
      <ChangeRequestsSection
        incidentId={detail.incident.id}
        changeRequests={[detail.change_request]}
        canManage={detail.can_manage}
        showComposer={false}
      />
    </Card>
    <ChangeHistory changeRequest={detail.change_request} />
  </main>
}

function ChangeHistory({changeRequest}: {changeRequest: ChangeRequest}) {
  return <section className="border-b py-4" aria-labelledby="change-history-title">
    <h2 id="change-history-title" className="text-sm font-semibold">变更历史</h2>
    {changeRequest.revisions.length ? <ol className="mt-3 divide-y border-y" aria-label="Plan revision 历史">
      {changeRequest.revisions.map((revision) => <li
        key={revision.id}
        className="grid min-w-0 gap-1 py-2 text-xs sm:grid-cols-[5rem_7rem_minmax(0,1fr)]"
      >
        <span>Revision {revision.number}</span>
        <Badge variant="outline">{revision.status}</Badge>
        <span className="min-w-0 break-words">
          {revision.plan?.summary ?? revision.question ?? "无可见 Plan"}
        </span>
      </li>)}
    </ol> : null}
    {changeRequest.events.length ? <ol className="mt-3 divide-y border-y" aria-label="Governance event 历史">
      {changeRequest.events.map((event) => <li
        key={event.id}
        className="grid min-w-0 gap-1 py-2 text-xs sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,0.7fr)]"
      >
        <time className="text-muted-foreground" dateTime={new Date(event.created_at * 1000).toISOString()}>
          {new Date(event.created_at * 1000).toLocaleString("zh-CN")}
        </time>
        <span className="min-w-0 break-words">{eventLabel[event.type] ?? event.type}</span>
        <span className="min-w-0 break-all text-muted-foreground">
          {event.actor_id ? <>Actor <MonoValue>{event.actor_id}</MonoValue></> : "System"}
        </span>
      </li>)}
    </ol> : <p className="mt-2 text-xs text-muted-foreground">尚无治理事件</p>}
  </section>
}

export function ChangeCenterPage() {
  const {changeRequestId} = useParams()
  const [params, setParams] = useSearchParams()
  const filters = changeCenterFilters(params)
  const listing = useQuery({
    queryKey: ["change-center"], queryFn: listChangeCenter,
    enabled: !changeRequestId,
  })
  const detail = useQuery({
    queryKey: ["change-center", changeRequestId],
    queryFn: () => getChangeCenterDetail(changeRequestId!),
    enabled: Boolean(changeRequestId),
  })

  if (changeRequestId) {
    if (detail.isPending) return <PageStatus>正在加载变更</PageStatus>
    if (detail.isError) return <PageStatus error>无法加载变更</PageStatus>
    return <ChangeCenterDetailView detail={detail.data} />
  }
  if (listing.isPending) return <PageStatus>正在加载变更</PageStatus>
  if (listing.isError) return <PageStatus error>无法加载变更</PageStatus>
  return <ChangeCenterListView
    changeRequests={listing.data.change_requests}
    pendingCount={listing.data.pending_count}
    filters={filters}
    onFiltersChange={(next) => setParams(filterSearch(next).slice(1), {replace: true})}
  />
}

function PageStatus({children, error = false}: {children: string; error?: boolean}) {
  if (!error) return <ListSkeleton />
  return <main
    className="grid min-h-[60vh] place-items-center text-sm text-destructive"
    role="status"
  >{children}</main>
}
