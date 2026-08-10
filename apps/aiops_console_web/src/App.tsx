import { lazy, Suspense } from "react"
import { useQuery } from "@tanstack/react-query"
import { BrowserRouter, Navigate, Route, Routes } from "react-router"
import { LoaderCircleIcon } from "lucide-react"

import { ApiError } from "@/api/transport"
import { getActor } from "@/auth/auth-client"
import { LoginPage } from "@/auth/login-page"
import { DetailSkeleton, ListSkeleton } from "@/components/page-skeleton"
import { TooltipProvider } from "@/components/ui/tooltip"
import { IncidentsPrototypePage } from "@/prototype/incidents-page"
import { WorkbenchPrototypePage } from "@/prototype/workbench-page"
import { ConsoleShell } from "@/shell/console-shell"

const AdminPage = lazy(() => import("@/admin/admin-page").then((module) => ({default: module.AdminPage})))
const ChatPage = lazy(() => import("@/chat/chat-page").then((module) => ({default: module.ChatPage})))
const ChangeCenterPage = lazy(() => import("@/changes/change-center-page").then((module) => ({default: module.ChangeCenterPage})))
const IncidentReportPage = lazy(() => import("@/reports/report-page").then((module) => ({default: module.IncidentReportPage})))
const ReportLibraryPage = lazy(() => import("@/reports/report-library-page").then((module) => ({default: module.ReportLibraryPage})))
const ResourceWorkspacePage = lazy(() => import("@/resources/resource-workspace-page").then((module) => ({default: module.ResourceWorkspacePage})))
const PlatformStatusPage = lazy(() => import("@/platform/platform-status-page").then((module) => ({default: module.PlatformStatusPage})))

function AuthenticatedApp() {
  const actor = useQuery({queryKey: ["actor"], queryFn: getActor, retry: false})

  if (actor.isPending) {
    return (
      <main className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">
        <span className="inline-flex items-center gap-2"><LoaderCircleIcon className="size-4 animate-spin motion-reduce:animate-none" />正在验证身份</span>
      </main>
    )
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
      <Route element={<ConsoleShell actor={actor.data} />}>
        <Route path="/chat" element={<Suspense fallback={<DetailSkeleton />}><ChatPage /></Suspense>} />
        <Route path="/chat/:sessionId" element={<Suspense fallback={<DetailSkeleton />}><ChatPage /></Suspense>} />
        <Route path="/incidents" element={<IncidentsPrototypePage />} />
        <Route path="/incidents/:incidentId" element={<WorkbenchPrototypePage />} />
        <Route path="/changes" element={<Suspense fallback={<ListSkeleton />}><ChangeCenterPage /></Suspense>} />
        <Route path="/changes/:changeRequestId" element={<Suspense fallback={<DetailSkeleton />}><ChangeCenterPage /></Suspense>} />
        <Route path="/reports" element={<Suspense fallback={<ListSkeleton />}><ReportLibraryPage /></Suspense>} />
        <Route path="/resources" element={<Suspense fallback={<ListSkeleton />}><ResourceWorkspacePage /></Suspense>} />
        <Route path="/platform" element={<Suspense fallback={<DetailSkeleton />}><PlatformStatusPage actor={actor.data} /></Suspense>} />
        <Route path="/incidents/:incidentId/report" element={<Suspense fallback={<DetailSkeleton />}><IncidentReportPage /></Suspense>} />
        <Route
          path="/admin"
          element={actor.data.is_platform_administrator ? (
            <Suspense fallback={<DetailSkeleton />}>
              <AdminPage />
            </Suspense>
          ) : <Navigate to="/incidents" replace />}
        />
      </Route>
      <Route path="*" element={<Navigate to="/incidents" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          <Route path="*" element={<AuthenticatedApp />} />
        </Routes>
      </BrowserRouter>
    </TooltipProvider>
  )
}
