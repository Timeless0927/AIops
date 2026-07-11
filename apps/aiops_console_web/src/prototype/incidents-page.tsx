import { useQuery } from "@tanstack/react-query"
import { ActivityIcon } from "lucide-react"
import { Link } from "react-router"

import { listIncidents } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ConsoleHeader, MonoValue } from "@/prototype/shared"

const statusLabel = {active: "处理中", resolved: "已解决"}
const bindingLabel = {bound: "已绑定", unbound: "未绑定"}

export function IncidentsPrototypePage() {
  const incidents = useQuery({queryKey: ["incidents"], queryFn: listIncidents})

  return (
    <div className="min-h-screen bg-background">
      <ConsoleHeader />
      <main className="mx-auto max-w-[1500px] px-4 py-6 lg:px-6 lg:py-8">
        <header className="border-b pb-5">
          <h1 className="text-2xl font-semibold sm:text-3xl">事件</h1>
        </header>
        {incidents.isPending ? (
          <div className="py-12 text-sm text-muted-foreground" role="status">正在加载事件</div>
        ) : incidents.isError ? (
          <Empty className="mt-6 min-h-72 border">
            <EmptyHeader>
              <EmptyTitle>无法加载事件</EmptyTitle>
              <EmptyDescription>请刷新页面重试。</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : incidents.data.length === 0 ? (
          <Empty className="mt-6 min-h-72 border">
            <EmptyHeader>
              <EmptyMedia variant="icon"><ActivityIcon /></EmptyMedia>
              <EmptyTitle>暂无事件</EmptyTitle>
              <EmptyDescription>收到有效告警后，事件会显示在这里。</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <ul className="divide-y" aria-label="事件列表">
            {incidents.data.map((incident) => (
              <li key={incident.id} className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <Link to={`/incidents/${incident.id}`} className="font-medium hover:underline">
                    {incident.title}
                  </Link>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <MonoValue>{incident.id}</MonoValue>
                    <span>{incident.cluster_name} / {incident.namespace}</span>
                    <span>{incident.signal_count} 个信号</span>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge variant={incident.status === "active" ? "default" : "secondary"}>
                    {statusLabel[incident.status]}
                  </Badge>
                  <Badge variant="outline">{bindingLabel[incident.binding_status]}</Badge>
                </div>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  )
}
