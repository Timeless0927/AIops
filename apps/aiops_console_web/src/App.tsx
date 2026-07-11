import { FileTextIcon, SettingsIcon } from "lucide-react"
import { useQuery } from "@tanstack/react-query"
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router"

import { ApiError, getActor } from "@/api/client"
import { LoginPage } from "@/auth/login-page"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { TooltipProvider } from "@/components/ui/tooltip"
import { IncidentsPrototypePage } from "@/prototype/incidents-page"
import { ConsoleHeader } from "@/prototype/shared"
import { WorkbenchPrototypePage } from "@/prototype/workbench-page"

function AdminPage() {
  return (
    <div className="min-h-screen bg-background">
      <ConsoleHeader />
      <main className="mx-auto max-w-[1500px] px-4 py-8 lg:px-6">
        <h1 className="text-2xl font-semibold">平台管理</h1>
        <Empty className="mt-6 min-h-72 border">
          <EmptyHeader>
            <EmptyMedia variant="icon"><SettingsIcon /></EmptyMedia>
            <EmptyTitle>暂无可管理资源</EmptyTitle>
            <EmptyDescription>完成身份与资源注册后，管理项会显示在这里。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </main>
    </div>
  )
}

function IncidentReportPage() {
  const { incidentId } = useParams()

  return (
    <div className="min-h-screen bg-background">
      <ConsoleHeader showBack backTo={`/incidents/${incidentId}`} />
      <main className="mx-auto max-w-[1100px] px-4 py-8 lg:px-6">
        <h1 className="text-2xl font-semibold">事件报告</h1>
        <Empty className="mt-6 min-h-72 border">
          <EmptyHeader>
            <EmptyMedia variant="icon"><FileTextIcon /></EmptyMedia>
            <EmptyTitle>报告尚未生成</EmptyTitle>
            <EmptyDescription>事件解决且调查结束后，可在这里查看报告。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </main>
    </div>
  )
}

function AuthenticatedApp() {
  const actor = useQuery({queryKey: ["actor"], queryFn: getActor, retry: false})

  if (actor.isPending) {
    return <main className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">正在验证身份</main>
  }
  if (actor.error instanceof ApiError && [401, 403].includes(actor.error.status)) {
    return <LoginPage />
  }
  if (actor.isError) {
    return <main className="grid min-h-screen place-items-center text-sm text-destructive">无法连接 Gateway</main>
  }

  return (
    <Routes>
      <Route path="/" element={<Navigate to="/incidents" replace />} />
      <Route path="/login" element={<Navigate to="/incidents" replace />} />
      <Route path="/incidents" element={<IncidentsPrototypePage />} />
      <Route path="/incidents/:incidentId" element={<WorkbenchPrototypePage />} />
      <Route path="/incidents/:incidentId/report" element={<IncidentReportPage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="*" element={<Navigate to="/incidents" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <AuthenticatedApp />
      </BrowserRouter>
    </TooltipProvider>
  )
}
