import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowLeftIcon } from "lucide-react"
import { Link, Outlet, useLocation, useNavigate } from "react-router"

import { AppSidebar } from "@/components/app-sidebar"
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage } from "@/components/ui/breadcrumb"
import { buttonVariants } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar"
import { logout, type Actor } from "@/auth/auth-client"
import { cn } from "@/lib/utils"

export function shellRoute(pathname: string, search = "") {
  const report = pathname.match(/^\/incidents\/([^/]+)\/report\/?$/)
  if (report) {
    const params = new URLSearchParams(search)
    if (params.get("from") !== "reports") {
      return {
        chatActive: false,
        incidentsActive: true,
        changesActive: false,
        reportsActive: false,
        resourcesActive: false,
        platformActive: false,
        backTo: `/incidents/${report[1]}`,
        backLabel: "返回事件工作区",
      }
    }
    params.delete("from")
    const filters = params.toString()
    return {
      chatActive: false,
      incidentsActive: false,
      changesActive: false,
      reportsActive: true,
      resourcesActive: false,
      platformActive: false,
      backTo: filters ? `/reports?${filters}` : "/reports",
      backLabel: "返回报告列表",
    }
  }
  if (/^\/incidents\/[^/]+\/?$/.test(pathname)) {
    return {
      chatActive: false,
      incidentsActive: true,
      changesActive: false,
      reportsActive: false,
      resourcesActive: false,
      platformActive: false,
      backTo: "/incidents",
      backLabel: "返回事件列表",
    }
  }
  if (/^\/changes\/[^/]+\/?$/.test(pathname)) {
    return {
      chatActive: false,
      incidentsActive: false,
      changesActive: true,
      reportsActive: false,
      resourcesActive: false,
      platformActive: false,
      backTo: "/changes",
      backLabel: "返回变更列表",
    }
  }
  return {
    chatActive: pathname === "/chat" || pathname === "/chat/" || pathname.startsWith("/chat/"),
    incidentsActive: pathname === "/incidents" || pathname === "/incidents/",
    changesActive: pathname === "/changes" || pathname === "/changes/",
    reportsActive: pathname === "/reports" || pathname === "/reports/",
    resourcesActive: pathname === "/resources" || pathname === "/resources/",
    platformActive: pathname === "/platform" || pathname === "/platform/",
  }
}

function pageLabel(pathname: string) {
  if (pathname.startsWith("/chat")) return "AI 对话"
  if (pathname.startsWith("/incidents")) return pathname.endsWith("/report") ? "事件报告" : "事件"
  if (pathname.startsWith("/changes")) return "变更"
  if (pathname.startsWith("/reports")) return "报告"
  if (pathname.startsWith("/resources")) return "资源"
  if (pathname.startsWith("/platform")) return "平台状态"
  if (pathname.startsWith("/admin")) return "平台管理"
  return "控制台"
}

export function ConsoleShell({actor}: {actor: Actor}) {
  const {pathname, search} = useLocation()
  const route = shellRoute(pathname, search)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      queryClient.clear()
      navigate("/login", {replace: true})
    },
  })

  return (
    <SidebarProvider className="min-h-screen min-w-0 overflow-x-hidden bg-background">
      <AppSidebar actor={actor} onLogout={() => logoutMutation.mutate()} />
      <SidebarInset className="min-w-0 overflow-x-hidden">
        <header className="flex min-h-13 min-w-0 items-center gap-2 border-b px-3 sm:px-4 lg:px-6">
          <SidebarTrigger aria-label="切换侧栏" />
          <Separator orientation="vertical" className="h-4" />
          {route.backTo ? (
            <Link
              to={route.backTo}
              className={cn(buttonVariants({variant: "ghost", size: "icon-sm"}), "shrink-0")}
              aria-label={route.backLabel}
            >
              <ArrowLeftIcon />
            </Link>
          ) : null}
          <Breadcrumb aria-label="当前位置">
            <BreadcrumbList>
              <BreadcrumbItem>
                <BreadcrumbPage>{pageLabel(pathname)}</BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
        </header>
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  )
}
