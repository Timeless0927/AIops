import type { ReactNode } from "react"
import {
  ActivityIcon,
  BoxesIcon,
  FileTextIcon,
  GaugeIcon,
  GitPullRequestIcon,
  MessageCircleIcon,
  Settings2Icon,
  TriangleAlertIcon,
} from "lucide-react"
import { useLocation } from "react-router"

import type { Actor } from "@/auth/auth-client"
import { NavMain } from "@/components/nav-main"
import { NavUser } from "@/components/nav-user"
import { TeamSwitcher } from "@/components/team-switcher"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarRail,
} from "@/components/ui/sidebar"

type ConsoleNavItem = {
  title: string
  url: string
  icon: ReactNode
  isActive?: boolean
}

function isActivePath(pathname: string, url: string) {
  return pathname === url || pathname.startsWith(`${url}/`)
}

export function AppSidebar({
  actor,
  onLogout,
  ...props
}: React.ComponentProps<typeof Sidebar> & {
  actor: Actor
  onLogout: () => void
}) {
  const { pathname } = useLocation()
  const items: ConsoleNavItem[] = [
    {title: "AI 对话", url: "/chat", icon: <MessageCircleIcon />, isActive: isActivePath(pathname, "/chat")},
    {title: "事件", url: "/incidents", icon: <TriangleAlertIcon />, isActive: isActivePath(pathname, "/incidents")},
    {title: "变更", url: "/changes", icon: <GitPullRequestIcon />, isActive: isActivePath(pathname, "/changes")},
    {title: "报告", url: "/reports", icon: <FileTextIcon />, isActive: isActivePath(pathname, "/reports")},
    {title: "资源", url: "/resources", icon: <BoxesIcon />, isActive: isActivePath(pathname, "/resources")},
    {title: "平台状态", url: "/platform", icon: <GaugeIcon />, isActive: isActivePath(pathname, "/platform")},
  ]
  if (actor.is_platform_administrator) {
    items.push({title: "平台管理", url: "/admin", icon: <Settings2Icon />, isActive: isActivePath(pathname, "/admin")})
  }

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader>
        <TeamSwitcher
          teams={[{name: "AIOps 控制台", logo: <ActivityIcon />, plan: actor.display_name || actor.username}]}
        />
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={items} />
      </SidebarContent>
      <SidebarFooter>
        <NavUser actor={actor} onLogout={onLogout} />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
