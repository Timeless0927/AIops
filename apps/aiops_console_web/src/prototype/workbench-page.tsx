import { useQuery } from "@tanstack/react-query"
import { ActivityIcon, FileTextIcon, ServerIcon, ShieldCheckIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import { getIncidentWorkbench } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ConsoleHeader, MonoValue } from "@/prototype/shared"

const incidentStatus = {active: "处理中", resolved: "已解决"}
const bindingStatus = {bound: "已绑定资源", unbound: "资源未绑定"}
const signalStatus = {firing: "告警中", recovered: "已恢复"}
const investigationStatus = {
  queued: "等待调查",
  running: "调查中",
  paused: "已暂停",
  human_led: "人工接管",
  completed: "已完成",
  failed: "失败",
  terminated: "已终止",
}

function LoadingState() {
  return <main className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground" role="status">正在加载事件</main>
}

function ErrorState() {
  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8 lg:px-6">
      <Empty className="min-h-72 border">
        <EmptyHeader>
          <EmptyMedia variant="icon"><ActivityIcon /></EmptyMedia>
          <EmptyTitle>无法加载事件</EmptyTitle>
          <EmptyDescription>事件不存在，或当前账号没有访问权限。</EmptyDescription>
        </EmptyHeader>
      </Empty>
    </main>
  )
}

export function WorkbenchPrototypePage() {
  const { incidentId = "" } = useParams()
  const workbench = useQuery({
    queryKey: ["incidents", incidentId, "workbench"],
    queryFn: () => getIncidentWorkbench(incidentId),
    enabled: Boolean(incidentId),
  })

  if (workbench.isPending) return <div className="min-h-screen bg-background"><ConsoleHeader showBack /><LoadingState /></div>
  if (workbench.isError) return <div className="min-h-screen bg-background"><ConsoleHeader showBack /><ErrorState /></div>

  const snapshot = workbench.data
  const { incident, resource_context: resource, responsibility, investigation } = snapshot

  return (
    <div className="min-h-screen bg-background pb-8">
      <ConsoleHeader showBack />
      <section className="border-b bg-surface" aria-labelledby="incident-title">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-4 py-5 lg:px-6">
          <div className="flex flex-wrap items-center gap-2">
            <MonoValue>{incident.id}</MonoValue>
            <Badge>{incidentStatus[incident.status]}</Badge>
            <Badge variant="outline">{bindingStatus[incident.binding_status]}</Badge>
            <Badge variant="secondary">{incident.severity}</Badge>
            <Link to={`/incidents/${incident.id}/report`} className="ml-auto inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
              <FileTextIcon className="size-4" />事件报告
            </Link>
          </div>
          <div>
            <h1 id="incident-title" className="text-2xl font-semibold">{incident.title}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {incident.alertname} · {incident.signal_count} 个 Alert Signal
            </p>
          </div>
        </div>
      </section>

      <main className="mx-auto max-w-[1500px] px-4 py-5 lg:px-6">
        <div className="overflow-hidden rounded-md border lg:grid lg:grid-cols-[300px_minmax(0,1fr)]">
          <aside className="border-b bg-surface p-4 lg:border-r lg:border-b-0" aria-labelledby="facts-title">
            <h2 id="facts-title" className="text-sm font-medium">事件事实</h2>
            <dl className="mt-3 divide-y text-sm">
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">Cluster / Namespace</dt>
                <dd className="mt-1 break-words font-medium">{resource.cluster_name} / {resource.namespace}</dd>
                <dd className="mt-1"><MonoValue>{resource.environment} · {resource.runtime_status}</MonoValue></dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">Deployment Target</dt>
                <dd className="mt-1 break-words font-medium">{resource.workload_name ?? "无法解析"}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">{resource.workload_kind ?? "未知工作负载类型"}</dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">Service / Team</dt>
                <dd className="mt-1 break-words font-medium">{resource.service_name ?? "未绑定 Service"}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">{responsibility.team_name ?? "责任团队待确认"}</dd>
              </div>
              <div className="py-3">
                <dt className="text-xs text-muted-foreground">调查状态</dt>
                <dd className="mt-1 flex items-center gap-2 font-medium">
                  <ShieldCheckIcon className="size-4 text-muted-foreground" />
                  {investigation ? investigationStatus[investigation.status] : "尚未建立调查"}
                </dd>
              </div>
            </dl>
            <div className="mt-5 flex items-center gap-2 text-xs text-muted-foreground">
              <ServerIcon className="size-4" />快照 r{snapshot.snapshot_revision} · cursor {snapshot.event_cursor}
            </div>
          </aside>

          <section className="min-w-0" aria-labelledby="signals-title">
            <header className="border-b p-4">
              <h2 id="signals-title" className="text-base font-semibold">Alert Signals</h2>
              <p className="mt-1 text-xs text-muted-foreground">独立保留每个 Alertmanager fingerprint</p>
            </header>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Alert</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">Fingerprint</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.alert_signals.map((signal) => (
                  <TableRow key={signal.fingerprint}>
                    <TableCell className="max-w-[520px] whitespace-normal py-3">
                      <div className="font-medium">{signal.alertname}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{signal.summary || "无摘要"}</div>
                    </TableCell>
                    <TableCell className="whitespace-normal">{signal.workload_name ?? "未解析"}</TableCell>
                    <TableCell><Badge variant={signal.status === "firing" ? "destructive" : "secondary"}>{signalStatus[signal.status]}</Badge></TableCell>
                    <TableCell className="text-right"><MonoValue>{signal.fingerprint}</MonoValue></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
        </div>
      </main>
    </div>
  )
}
