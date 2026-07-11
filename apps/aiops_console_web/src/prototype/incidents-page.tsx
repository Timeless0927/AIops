import { useMemo } from "react"
import {
  ActivityIcon,
  ArrowUpRightIcon,
  CircleDotIcon,
  SearchIcon,
  ShieldAlertIcon,
} from "lucide-react"
import { Link, useSearchParams } from "react-router"

import { buttonVariants } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Separator } from "@/components/ui/separator"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { incidents, type IncidentFixture } from "@/prototype/data"
import {
  readIncidentListState,
  updateIncidentListSearch,
  type IncidentFilter,
} from "@/prototype/incident-list-state"
import {
  ConsoleHeader,
  IncidentStateBadge,
  MonoValue,
  SeverityBadge,
} from "@/prototype/shared"

function matchesFilter(incident: IncidentFixture, filter: IncidentFilter) {
  if (filter === "resolved") return incident.state === "resolved"
  if (filter === "waiting") return incident.state === "waiting"
  return incident.state !== "resolved"
}

export function IncidentsPrototypePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { filter, query } = readIncidentListState(searchParams)

  const updateSearch = (key: "q" | "status", value: string) => {
    setSearchParams(updateIncidentListSearch(searchParams, key, value), { replace: true })
  }

  const visibleIncidents = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return incidents.filter((incident) => {
      const searchable = [
        incident.id,
        incident.title,
        incident.service,
        incident.namespace,
        incident.cluster,
        incident.team,
      ]
        .join(" ")
        .toLowerCase()
      return matchesFilter(incident, filter) && searchable.includes(normalized)
    })
  }, [filter, query])

  return (
    <div className="min-h-screen bg-background">
      <ConsoleHeader />

      <main className="mx-auto max-w-[1500px] px-4 py-6 lg:px-6 lg:py-8">
        <header className="flex flex-col gap-4 border-b pb-5 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
              <CircleDotIcon className="size-3.5 text-evidence" />
              <span>值班队列</span>
              <span aria-hidden="true">/</span>
              <span>华东生产域</span>
            </div>
            <h1 className="text-2xl font-semibold sm:text-3xl">事件</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              按影响与等待时间排序，优先处理仍在扩大的故障。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span><strong className="font-mono text-lg">3</strong> 调查中</span>
            <Separator orientation="vertical" className="h-5" />
            <span><strong className="font-mono text-lg text-warning-foreground">1</strong> 等待证据</span>
            <Separator orientation="vertical" className="h-5" />
            <span><strong className="font-mono text-lg text-destructive">1</strong> 严重</span>
          </div>
        </header>

        <section className="mt-5" aria-labelledby="incident-queue-title">
          <h2 id="incident-queue-title" className="sr-only">事件队列</h2>

          <div className="flex flex-col gap-3 pb-4 md:flex-row md:items-center md:justify-between">
            <ToggleGroup
              value={[filter]}
              onValueChange={(values) => values[0] && updateSearch("status", values[0])}
              variant="outline"
              size="sm"
              aria-label="事件状态筛选"
            >
              <ToggleGroupItem value="active">进行中</ToggleGroupItem>
              <ToggleGroupItem value="waiting">等待证据</ToggleGroupItem>
              <ToggleGroupItem value="resolved">已解决</ToggleGroupItem>
            </ToggleGroup>

            <InputGroup className="w-full md:w-80">
              <InputGroupInput
                value={query}
                onChange={(event) => updateSearch("q", event.target.value)}
                placeholder="搜索事件、服务或集群"
                aria-label="搜索事件"
              />
              <InputGroupAddon>
                <SearchIcon />
              </InputGroupAddon>
            </InputGroup>
          </div>

          {visibleIncidents.length ? (
            <>
              <div className="hidden overflow-hidden rounded-md border md:block">
                <Table>
                  <TableHeader className="bg-surface">
                    <TableRow>
                      <TableHead className="w-20">级别</TableHead>
                      <TableHead>事件</TableHead>
                      <TableHead>目标资源</TableHead>
                      <TableHead className="w-28">状态</TableHead>
                      <TableHead className="w-28">负责人</TableHead>
                      <TableHead className="w-24 text-right">持续</TableHead>
                      <TableHead className="w-12"><span className="sr-only">打开</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {visibleIncidents.map((incident) => (
                      <TableRow key={incident.id}>
                        <TableCell><SeverityBadge severity={incident.severity} /></TableCell>
                        <TableCell className="max-w-[440px] whitespace-normal py-3">
                          <Link
                            to={`/incidents/${incident.id}`}
                            className="font-medium hover:underline hover:underline-offset-4"
                          >
                            {incident.title}
                          </Link>
                          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                            <MonoValue>{incident.id}</MonoValue>
                            <span>{incident.signalCount} 个信号</span>
                            {!incident.bound ? (
                              <span className="inline-flex items-center gap-1 text-warning-foreground">
                                <ShieldAlertIcon className="size-3.5" />资源未绑定
                              </span>
                            ) : null}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-[320px] whitespace-normal">
                          <div className="font-medium">{incident.service}</div>
                          <div className="mt-0.5 font-mono text-xs text-muted-foreground">
                            {incident.environment} / {incident.namespace} / {incident.cluster}
                          </div>
                        </TableCell>
                        <TableCell><IncidentStateBadge state={incident.state} /></TableCell>
                        <TableCell>
                          <div>{incident.owner}</div>
                          <div className="text-xs text-muted-foreground">{incident.team}</div>
                        </TableCell>
                        <TableCell className="text-right"><MonoValue>{incident.duration}</MonoValue></TableCell>
                        <TableCell className="text-right">
                          <Link
                            to={`/incidents/${incident.id}`}
                            className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
                            aria-label={`打开 ${incident.id}`}
                          >
                            <ArrowUpRightIcon />
                          </Link>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div className="divide-y rounded-md border md:hidden">
                {visibleIncidents.map((incident) => (
                  <article key={incident.id} className="p-4">
                    <div className="flex items-start gap-3">
                      <div className="flex min-w-0 flex-1 flex-col gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <SeverityBadge severity={incident.severity} />
                          <IncidentStateBadge state={incident.state} />
                          <MonoValue className="text-muted-foreground">{incident.id}</MonoValue>
                        </div>
                        <Link
                          to={`/incidents/${incident.id}`}
                          className="text-base font-medium leading-6"
                        >
                          {incident.title}
                        </Link>
                        <p className="text-sm leading-5 text-muted-foreground">{incident.summary}</p>
                        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                          <span>{incident.environment} / {incident.namespace}</span>
                          <span>{incident.owner}</span>
                          <MonoValue>{incident.duration}</MonoValue>
                        </div>
                      </div>
                      <Link
                        to={`/incidents/${incident.id}`}
                        className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
                        aria-label={`打开 ${incident.id}`}
                      >
                        <ArrowUpRightIcon />
                      </Link>
                    </div>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <Empty className="min-h-80 border">
              <EmptyHeader>
                <EmptyMedia variant="icon"><ActivityIcon /></EmptyMedia>
                <EmptyTitle>没有匹配的事件</EmptyTitle>
                <EmptyDescription>调整状态筛选或搜索范围。</EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </section>
      </main>
    </div>
  )
}
