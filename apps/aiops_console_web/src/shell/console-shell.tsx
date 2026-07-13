import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  ActivityIcon,
  ArrowLeftIcon,
  LogOutIcon,
  MenuIcon,
  SettingsIcon,
  UserRoundIcon,
} from "lucide-react"
import { Link, Outlet, useLocation, useNavigate } from "react-router"

import { logout, type Actor } from "@/api/client"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

export function shellRoute(pathname: string) {
  const report = pathname.match(/^\/incidents\/([^/]+)\/report\/?$/)
  if (report) {
    return {
      incidentsActive: true,
      changesActive: false,
      backTo: `/incidents/${report[1]}`,
      backLabel: "返回事件工作区",
    }
  }
  if (/^\/incidents\/[^/]+\/?$/.test(pathname)) {
    return {
      incidentsActive: true,
      changesActive: false,
      backTo: "/incidents",
      backLabel: "返回事件列表",
    }
  }
  if (/^\/changes\/[^/]+\/?$/.test(pathname)) {
    return {
      incidentsActive: false,
      changesActive: true,
      backTo: "/changes",
      backLabel: "返回变更列表",
    }
  }
  return {
    incidentsActive: pathname === "/incidents" || pathname === "/incidents/",
    changesActive: pathname === "/changes" || pathname === "/changes/",
  }
}

export function ConsoleShell({actor}: {actor: Actor}) {
  const {pathname} = useLocation()
  const route = shellRoute(pathname)
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
    <div className="min-h-screen min-w-0 overflow-x-hidden bg-background">
      <header className="border-b bg-background">
        <div className="mx-auto flex h-13 max-w-[1600px] min-w-0 items-center gap-2 px-3 sm:gap-3 sm:px-4 lg:px-6">
          {route.backTo ? (
            <Link
              to={route.backTo}
              className={buttonVariants({variant: "ghost", size: "icon-sm"})}
              aria-label={route.backLabel}
            >
              <ArrowLeftIcon />
            </Link>
          ) : null}

          <Link to="/incidents" className="flex min-w-0 items-center gap-2.5 text-sm font-medium">
            <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-background">
              <ActivityIcon />
            </span>
            <span className="hidden truncate sm:inline">AIOps Control Plane</span>
            <span className="font-mono text-[11px] text-muted-foreground sm:hidden">AIOps</span>
          </Link>

          <nav aria-label="主导航" className="ml-2 hidden self-stretch sm:flex">
            <Link
              to="/incidents"
              aria-current={route.incidentsActive ? "page" : undefined}
              className={cn(
                "flex items-center border-b-2 px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                route.incidentsActive ? "border-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              事件
            </Link>
            <Link
              to="/changes"
              aria-current={route.changesActive ? "page" : undefined}
              className={cn(
                "flex items-center border-b-2 px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                route.changesActive ? "border-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              变更
            </Link>
          </nav>

          <div className="ml-auto flex shrink-0 items-center gap-1">
            <Sheet>
              <SheetTrigger
                render={<Button variant="ghost" size="icon-sm" className="sm:hidden" aria-label="打开主导航" />}
              >
                <MenuIcon />
              </SheetTrigger>
              <SheetContent side="left" className="max-w-72 sm:hidden">
                <SheetHeader>
                  <SheetTitle>导航</SheetTitle>
                </SheetHeader>
                <nav aria-label="移动端主导航" className="px-4">
                  <SheetClose
                    render={
                      <Link
                        to="/incidents"
                        className={buttonVariants({
                          variant: route.incidentsActive ? "secondary" : "ghost",
                          className: "w-full justify-start",
                        })}
                        aria-current={route.incidentsActive ? "page" : undefined}
                      />
                    }
                    nativeButton={false}
                  >
                    事件
                  </SheetClose>
                  <SheetClose
                    render={
                      <Link
                        to="/changes"
                        className={buttonVariants({
                          variant: route.changesActive ? "secondary" : "ghost",
                          className: "w-full justify-start",
                        })}
                        aria-current={route.changesActive ? "page" : undefined}
                      />
                    }
                    nativeButton={false}
                  >
                    变更
                  </SheetClose>
                </nav>
              </SheetContent>
            </Sheet>

            <DropdownMenu>
              <DropdownMenuTrigger
                render={<Button variant="ghost" size="icon-sm" aria-label="用户菜单" />}
              >
                <UserRoundIcon />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{actor.display_name || actor.username}</DropdownMenuLabel>
                  {actor.is_platform_administrator ? (
                    <DropdownMenuItem onClick={() => navigate("/admin")}>
                      <SettingsIcon />
                      平台管理
                    </DropdownMenuItem>
                  ) : null}
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={logoutMutation.isPending}
                    onClick={() => logoutMutation.mutate()}
                  >
                    <LogOutIcon />
                    退出登录
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </header>
      <Outlet />
    </div>
  )
}
