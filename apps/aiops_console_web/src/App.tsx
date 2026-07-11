import { lazy, Suspense } from "react"
import { useQuery } from "@tanstack/react-query"
import { BrowserRouter, Navigate, Route, Routes } from "react-router"

import { ApiError, getActor } from "@/api/client"
import { LoginPage } from "@/auth/login-page"
import { TooltipProvider } from "@/components/ui/tooltip"
import { IncidentsPrototypePage } from "@/prototype/incidents-page"
import { WorkbenchPrototypePage } from "@/prototype/workbench-page"

const AdminPage = lazy(() => import("@/admin/admin-page").then((module) => ({default: module.AdminPage})))
const IncidentReportPage = lazy(() => import("@/reports/report-page").then((module) => ({default: module.IncidentReportPage})))

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
      <Route path="/incidents/:incidentId/report" element={
        <Suspense fallback={<main className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">正在加载事件报告</main>}>
          <IncidentReportPage />
        </Suspense>
      } />
      <Route
        path="/admin"
        element={actor.data.is_platform_administrator ? (
          <Suspense fallback={<main className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">正在加载平台管理</main>}>
            <AdminPage />
          </Suspense>
        ) : <Navigate to="/incidents" replace />}
      />
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
