import { useQuery } from "@tanstack/react-query"
import { BoxesIcon, SettingsIcon } from "lucide-react"
import { Link, useSearchParams } from "react-router"

import { listResourceWorkspace, type ResourceWorkspace } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { buttonVariants } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

const environments = ["all", "prod", "staging", "dev", "test"] as const
const states = ["all", "available", "unavailable", "unbound", "deleted"] as const
const runtimes = ["all", "online", "offline", "pending_registration", "rotation_pending", "disabled"] as const
type Filters = {cluster: string; environment: typeof environments[number]; team: string; service: string; state: typeof states[number]; runtime: typeof runtimes[number]; resource: string}
const stateLabel: Record<typeof states[number], string> = {
  all: "全部状态", available: "可用", unavailable: "不可用", unbound: "未绑定", deleted: "已删除",
}

export function resourceFilters(params: URLSearchParams): Filters {
  const environment = params.get("environment")
  const state = params.get("state")
  return {
    environment: environments.includes(environment as Filters["environment"])
      ? environment as Filters["environment"] : "all",
    cluster: params.get("cluster")?.trim() || "all",
    team: params.get("team")?.trim() || "all",
    state: states.includes(state as Filters["state"]) ? state as Filters["state"] : "all",
    service: params.get("service")?.trim() || "all",
    resource: params.get("resource")?.trim() || "",
    runtime: runtimes.includes(params.get("runtime") as Filters["runtime"])
      ? params.get("runtime") as Filters["runtime"] : "all",
  }
}

export function filterResources(data: ResourceWorkspace, filters: Filters) {
  const clusterById = new Map(data.clusters.map((cluster) => [cluster.id, cluster]))
  return data.resources.filter((resource) => (
    (filters.environment === "all" || clusterById.get(resource.cluster_id)?.environment === filters.environment)
    && (filters.cluster === "all" || resource.cluster_id === filters.cluster)
    && (filters.team === "all" || resource.team_id === filters.team)
    && (filters.state === "all" || resource.availability === filters.state)
    && (filters.runtime === "all" || clusterById.get(resource.cluster_id)?.runtime_status === filters.runtime)
    && (filters.service === "all" || resource.service_id === filters.service)
  ))
}

function searchFor(filters: Filters) {
  const params = new URLSearchParams()
  if (filters.environment !== "all") params.set("environment", filters.environment)
  if (filters.cluster !== "all") params.set("cluster", filters.cluster)
  if (filters.team !== "all") params.set("team", filters.team)
  if (filters.state !== "all") params.set("state", filters.state)
  if (filters.service !== "all") params.set("service", filters.service)
  if (filters.resource) params.set("resource", filters.resource)
  if (filters.runtime !== "all") params.set("runtime", filters.runtime)
  return params
}

export function ResourceWorkspaceView({data, filters, onChange}: {
  data: ResourceWorkspace; filters: Filters; onChange?: (filters: Filters) => void
}) {
  const resources = filterResources(data, filters)
  const clusters = new Map(data.clusters.map((cluster) => [cluster.id, cluster]))
  const services = new Map(data.services.map((service) => [service.id, service]))
  const teams = Array.from(new Map(data.services.map((service) => [service.team_id, service.team_name])).entries())
  return <main className="mx-auto w-full max-w-[1600px] min-w-0 px-3 py-5 sm:px-4 lg:px-6">
    <header className="flex min-w-0 flex-wrap items-end gap-3 border-b pb-4">
      <div className="min-w-0 flex-1"><h1 className="text-xl font-semibold">资源</h1><p className="mt-1 text-sm text-muted-foreground">Cluster、Service 与 Deployment Target</p></div>
      {data.can_administer ? <Link to="/admin?section=catalog" className={buttonVariants({variant: "outline", size: "sm"})}><SettingsIcon data-icon="inline-start" />治理资源</Link> : null}
    </header>
    <section aria-label="资源筛选" className="flex min-w-0 flex-wrap gap-2 border-b py-3">
      <FilterSelect label="按环境筛选" value={filters.environment} values={environments} onChange={(environment) => onChange?.({...filters, environment: environment as Filters["environment"]})} />
      <FilterSelect label="按 Cluster 筛选" value={filters.cluster} values={["all", ...data.clusters.map((cluster) => cluster.id)]} itemLabel={(value) => value === "all" ? "全部 Cluster" : clusters.get(value)?.name ?? value} onChange={(cluster) => onChange?.({...filters, cluster})} />
      <FilterSelect label="按 Team 筛选" value={filters.team} values={["all", ...teams.map(([id]) => id)]} itemLabel={(value) => value === "all" ? "全部 Team" : teams.find(([id]) => id === value)?.[1] ?? value} onChange={(team) => onChange?.({...filters, team})} />
      <FilterSelect label="按状态筛选" value={filters.state} values={states} itemLabel={(value) => stateLabel[value as Filters["state"]]} onChange={(state) => onChange?.({...filters, state: state as Filters["state"]})} />
      <Select value={filters.service} onValueChange={(service) => onChange?.({...filters, service: service ?? "all"})}>
        <SelectTrigger aria-label="按 Service 筛选" className="min-w-40"><SelectValue>{filters.service === "all" ? "全部 Service" : services.get(filters.service)?.name ?? "未知 Service"}</SelectValue></SelectTrigger>
        <SelectContent><SelectGroup><SelectItem value="all">全部 Service</SelectItem>{data.services.map((service) => <SelectItem key={service.id} value={service.id}>{service.name}</SelectItem>)}</SelectGroup></SelectContent>
      </Select>
      <FilterSelect label="按运行状态筛选" value={filters.runtime} values={runtimes} onChange={(runtime) => onChange?.({...filters, runtime: runtime as Filters["runtime"]})} />
    </section>
    <section className="border-b py-4" aria-labelledby="resource-clusters-title"><h2 id="resource-clusters-title" className="text-sm font-semibold">Cluster</h2><ul className="mt-2 divide-y" aria-label="Cluster 列表">{data.clusters.map((cluster) => <li key={cluster.id} className="grid min-w-0 gap-1 py-2 text-xs sm:grid-cols-[minmax(0,1fr)_auto_auto]"><span className="break-all font-medium">{cluster.name} · {cluster.id}</span><span>{cluster.environment} · {cluster.runtime_status}</span><span className="text-muted-foreground">Read {cluster.read_verification}</span></li>)}</ul></section>
    <section className="border-b py-4" aria-labelledby="resource-services-title"><h2 id="resource-services-title" className="text-sm font-semibold">Service</h2><ul className="mt-2 divide-y" aria-label="Service 列表">{data.services.map((service) => <li key={service.id} className="grid min-w-0 gap-1 py-2 text-xs sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"><span className="break-words font-medium">{service.name}</span><span className="break-words text-muted-foreground">{service.team_name}</span><Badge variant={service.active ? "positive" : "destructive"}>{service.active ? "Active" : "Inactive"}</Badge></li>)}</ul></section>
    <h2 className="border-b py-3 text-sm font-semibold">Deployment Target</h2>
    {resources.length ? <ul className="divide-y border-b" aria-label="资源列表">{resources.map((resource) => {
      const cluster = clusters.get(resource.cluster_id)
      return <li key={resource.id}><Link
        to={{pathname: "/resources", search: `?${searchFor({...filters, resource: resource.id})}`}}
        aria-current={filters.resource === resource.id ? "true" : undefined}
        className={filters.resource === resource.id
          ? "grid min-w-0 gap-2 bg-muted px-1 py-4 outline-none focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[minmax(0,1.4fr)_minmax(12rem,0.8fr)_auto] sm:items-center sm:px-3"
          : "grid min-w-0 gap-2 px-1 py-4 outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[minmax(0,1.4fr)_minmax(12rem,0.8fr)_auto] sm:items-center sm:px-3"}
      >
        <div className="min-w-0"><div className="break-words text-sm font-medium">{resource.kind} / {resource.name}</div><div className="mt-1 break-all text-xs text-muted-foreground">{resource.cluster_id} · {resource.namespace}</div></div>
        <div className="flex min-w-0 flex-wrap gap-2"><Badge variant="outline">{services.get(resource.service_id ?? "")?.name ?? "未绑定 Service"}</Badge><Badge variant={resource.availability === "available" ? "positive" : resource.availability === "unavailable" ? "warning" : "destructive"}>{stateLabel[resource.availability]}</Badge></div>
        <div className="text-xs text-muted-foreground">{cluster?.environment} · {cluster?.runtime_status}</div>
      </Link></li>
    })}</ul> : <Empty className="min-h-64 border-b"><EmptyHeader><EmptyMedia variant="icon"><BoxesIcon /></EmptyMedia><EmptyTitle>没有匹配的资源</EmptyTitle><EmptyDescription>当前筛选范围内没有 Resource。</EmptyDescription></EmptyHeader></Empty>}
  </main>
}

function FilterSelect({label, value, values, itemLabel = (item) => item, onChange}: {label: string; value: string; values: readonly string[]; itemLabel?: (value: string) => string; onChange: (value: string) => void}) {
  return <Select value={value} onValueChange={(next) => onChange(next ?? "all")}><SelectTrigger aria-label={label} className="min-w-36"><SelectValue>{itemLabel(value)}</SelectValue></SelectTrigger><SelectContent><SelectGroup>{values.map((item) => <SelectItem key={item} value={item}>{itemLabel(item)}</SelectItem>)}</SelectGroup></SelectContent></Select>
}

export function ResourceWorkspacePage() {
  const [params, setParams] = useSearchParams()
  const filters = resourceFilters(params)
  const query = useQuery({queryKey: ["resource-workspace"], queryFn: listResourceWorkspace})
  if (query.isPending) return <main className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground" role="status">正在加载资源</main>
  if (query.isError) return <main className="grid min-h-[60vh] place-items-center text-sm text-destructive" role="status">无法加载资源</main>
  return <ResourceWorkspaceView data={query.data} filters={filters} onChange={(next) => setParams(searchFor({...next, resource: ""}), {replace: true})} />
}
