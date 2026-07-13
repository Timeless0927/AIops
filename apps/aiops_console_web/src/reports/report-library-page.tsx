import { useQuery } from "@tanstack/react-query"
import { FileCheck2Icon } from "lucide-react"
import { Link, useSearchParams } from "react-router"

import {
  listIncidentReportLibrary,
  type IncidentReportLibrarySummary,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const reportStates = ["all", "draft", "published", "reopened"] as const
const timeRanges = ["all", "24h", "7d", "30d"] as const

export type ReportLibraryFilters = {
  state: typeof reportStates[number]
  service: string
  incident: string
  time: typeof timeRanges[number]
}

const timeLabel: Record<ReportLibraryFilters["time"], string> = {
  all: "全部时间",
  "24h": "最近 24 小时",
  "7d": "最近 7 天",
  "30d": "最近 30 天",
}

const timeSeconds: Record<Exclude<ReportLibraryFilters["time"], "all">, number> = {
  "24h": 24 * 60 * 60,
  "7d": 7 * 24 * 60 * 60,
  "30d": 30 * 24 * 60 * 60,
}

const stateLabel: Record<ReportLibraryFilters["state"], string> = {
  all: "全部状态",
  draft: "待完成草稿",
  published: "已发布",
  reopened: "事件已重开",
}

export function reportLibraryFilters(params: URLSearchParams): ReportLibraryFilters {
  const state = params.get("state")
  const time = params.get("time")
  return {
    state: reportStates.includes(state as ReportLibraryFilters["state"])
      ? state as ReportLibraryFilters["state"] : "all",
    service: params.get("service")?.trim() || "all",
    incident: params.get("incident")?.trim() || "",
    time: timeRanges.includes(time as ReportLibraryFilters["time"])
      ? time as ReportLibraryFilters["time"] : "all",
  }
}

export function filterReportLibrary(
  reports: IncidentReportLibrarySummary[],
  filters: ReportLibraryFilters,
  now = Date.now() / 1000,
) {
  const incident = filters.incident.toLocaleLowerCase("zh-CN")
  const since = filters.time === "all" ? null : now - timeSeconds[filters.time]
  return reports.filter((report) => {
    const incidentMatches = !incident
      || report.incident.id.toLocaleLowerCase("zh-CN").includes(incident)
      || report.incident.title.toLocaleLowerCase("zh-CN").includes(incident)
    return (filters.state === "all" || report.state === filters.state)
      && (filters.service === "all" || report.service?.id === filters.service)
      && incidentMatches
      && (since === null || report.relevant_at >= since)
  })
}

function filterSearch(filters: ReportLibraryFilters) {
  const params = new URLSearchParams()
  if (filters.state !== "all") params.set("state", filters.state)
  if (filters.service !== "all") params.set("service", filters.service)
  if (filters.incident) params.set("incident", filters.incident)
  if (filters.time !== "all") params.set("time", filters.time)
  return params
}

export function ReportLibraryListView({
  reports,
  filters,
  onFiltersChange,
}: {
  reports: IncidentReportLibrarySummary[]
  filters: ReportLibraryFilters
  onFiltersChange?: (filters: ReportLibraryFilters) => void
}) {
  const filtered = filterReportLibrary(reports, filters)
  const detailParams = filterSearch(filters)
  detailParams.set("from", "reports")
  const services = Array.from(new Map(
    reports.flatMap((report) => report.service ? [[report.service.id, report.service.name] as const] : []),
  ).entries()).sort((left, right) => left[1].localeCompare(right[1], "zh-CN"))

  return <main className="mx-auto w-full max-w-[1600px] min-w-0 px-3 py-5 sm:px-4 lg:px-6">
    <header className="border-b pb-4">
      <h1 className="text-xl font-semibold">报告</h1>
      <p className="mt-1 text-sm text-muted-foreground">Incident Report 资料库</p>
    </header>

    <section aria-label="报告筛选" className="flex min-w-0 flex-wrap gap-2 border-b py-3">
      <Input
        value={filters.incident}
        onChange={(event) => onFiltersChange?.({...filters, incident: event.target.value})}
        aria-label="按 Incident 筛选"
        placeholder="Incident 标题或 ID"
        className="min-w-48 max-w-64"
      />
      <Select
        value={filters.state}
        onValueChange={(state) => onFiltersChange?.({
          ...filters, state: state as ReportLibraryFilters["state"],
        })}
      >
        <SelectTrigger aria-label="按报告状态筛选" className="min-w-36">
          <SelectValue>{stateLabel[filters.state]}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {reportStates.map((state) => <SelectItem key={state} value={state}>
              {stateLabel[state]}
            </SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
      <Select
        value={filters.time}
        onValueChange={(time) => onFiltersChange?.({
          ...filters, time: (time ?? "all") as ReportLibraryFilters["time"],
        })}
      >
        <SelectTrigger aria-label="按时间筛选" className="min-w-36">
          <SelectValue>{timeLabel[filters.time]}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {timeRanges.map((time) => <SelectItem key={time} value={time}>
              {timeLabel[time]}
            </SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
      <Select
        value={filters.service}
        onValueChange={(service) => onFiltersChange?.({...filters, service: service ?? "all"})}
      >
        <SelectTrigger aria-label="按 Service 筛选" className="min-w-40">
          <SelectValue>
            {filters.service === "all"
              ? "全部 Service"
              : services.find(([id]) => id === filters.service)?.[1] ?? "未知 Service"}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value="all">全部 Service</SelectItem>
            {services.map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
    </section>

    {filtered.length ? <ul className="divide-y border-b" aria-label="Incident Report 列表">
      {filtered.map((report) => <li key={report.incident.id}>
        <Link
          to={{
            pathname: `/incidents/${report.incident.id}/report`,
            search: `?${detailParams.toString()}`,
          }}
          className="grid min-w-0 gap-2 px-1 py-4 outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[minmax(0,1.4fr)_minmax(12rem,0.8fr)_auto] sm:items-center sm:px-3"
        >
          <div className="min-w-0">
            <div className="break-words text-sm font-medium">{report.incident.title}</div>
            <div className="mt-1 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span className="break-words">{report.service?.name ?? "未绑定 Service"}</span>
              <span>{report.incident.severity}</span>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap gap-2">
            <Badge variant={report.state === "draft" ? "warning" : "outline"}>
              {stateLabel[report.state]}
            </Badge>
            {report.latest_publication ? <Badge variant="secondary">
              最新版本 v{report.latest_publication.version}
            </Badge> : null}
            {report.publication_count ? <span className="self-center text-xs text-muted-foreground">
              {report.publication_count} 个发布版本
            </span> : null}
          </div>
          <time
            className="text-xs text-muted-foreground"
            dateTime={new Date(report.relevant_at * 1000).toISOString()}
          >
            {new Date(report.relevant_at * 1000).toLocaleString("zh-CN")}
          </time>
        </Link>
      </li>)}
    </ul> : <Empty className="min-h-64 border-b">
      <EmptyHeader>
        <EmptyMedia variant="icon"><FileCheck2Icon /></EmptyMedia>
        <EmptyTitle>没有事件报告</EmptyTitle>
        <EmptyDescription>当前筛选范围内没有草稿或已发布版本。</EmptyDescription>
      </EmptyHeader>
    </Empty>}
  </main>
}

export function ReportLibraryPage() {
  const [params, setParams] = useSearchParams()
  const filters = reportLibraryFilters(params)
  const listing = useQuery({
    queryKey: ["report-library"],
    queryFn: listIncidentReportLibrary,
  })

  if (listing.isPending) return <PageStatus>正在加载报告</PageStatus>
  if (listing.isError) return <PageStatus error>无法加载报告</PageStatus>
  return <ReportLibraryListView
    reports={listing.data.reports}
    filters={filters}
    onFiltersChange={(next) => setParams(filterSearch(next), {replace: true})}
  />
}

function PageStatus({children, error = false}: {children: string; error?: boolean}) {
  return <main
    className={error
      ? "grid min-h-[60vh] place-items-center text-sm text-destructive"
      : "grid min-h-[60vh] place-items-center text-sm text-muted-foreground"}
    role="status"
  >{children}</main>
}
