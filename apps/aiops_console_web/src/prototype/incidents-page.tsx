import { useQuery } from "@tanstack/react-query"
import { ActivityIcon } from "lucide-react"

import { listIncidents } from "@/api/client"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ConsoleHeader, MonoValue } from "@/prototype/shared"

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
              <li key={incident.id} className="flex items-center justify-between gap-4 py-4">
                <div>
                  <div className="font-medium">{incident.title}</div>
                  <MonoValue className="text-muted-foreground">{incident.id}</MonoValue>
                </div>
                <span className="text-sm text-muted-foreground">{incident.status}</span>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  )
}
