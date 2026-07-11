import {
  ActivityIcon,
  ArrowLeftIcon,
  EyeIcon,
  LogOutIcon,
  RadioIcon,
  SettingsIcon,
  UserRoundIcon,
} from "lucide-react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "react-router"

import { getActor, logout } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type {
  EvidenceRelation,
  EvidenceStatus,
  IncidentSeverity,
  IncidentState,
} from "@/prototype/data"

const severityLabels: Record<IncidentSeverity, string> = {
  critical: "严重",
  high: "高",
  medium: "中",
}

const stateLabels: Record<IncidentState, string> = {
  investigating: "调查中",
  stabilizing: "稳定观察",
  waiting: "等待证据",
  resolved: "已解决",
}

const relationLabels: Record<EvidenceRelation, string> = {
  supports: "支持判断",
  challenges: "反证",
  context: "上下文",
}

const statusLabels: Record<EvidenceStatus, string> = {
  verified: "已验证",
  partial: "部分证据",
  missing: "缺失",
}

export function ConsoleHeader({
  showBack = false,
  backTo = "/incidents",
}: {
  showBack?: boolean
  backTo?: string
}) {
  const actor = useQuery({queryKey: ["actor"], queryFn: getActor})
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: async () => {
      queryClient.clear()
      navigate("/login", {replace: true})
    },
  })

  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex h-13 max-w-[1600px] items-center gap-3 px-4 lg:px-6">
        {showBack ? (
          <Link
            to={backTo}
            className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
            aria-label="返回事件列表"
          >
            <ArrowLeftIcon />
          </Link>
        ) : null}

        <Link
          to="/incidents"
          className="flex min-w-0 items-center gap-2.5 text-sm font-medium"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-background">
            <ActivityIcon className="size-4" />
          </span>
          <span className="hidden sm:inline">AIOps Control Plane</span>
          <span className="font-mono text-[11px] text-muted-foreground sm:hidden">AIOps</span>
        </Link>

        <nav aria-label="主导航" className="ml-3 hidden items-center sm:flex">
          <Link
            to="/incidents"
            className="border-b-2 border-foreground px-3 py-4 text-sm font-medium"
          >
            事件
          </Link>
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <Badge variant="evidence" className="hidden sm:inline-flex">
            <RadioIcon data-icon="inline-start" />
            实时连接
          </Badge>
          <details className="group relative">
            <summary
              className={buttonVariants({ variant: "ghost", size: "icon-sm", className: "list-none" })}
              aria-label="用户菜单"
            >
              <UserRoundIcon />
            </summary>
            <div className="absolute right-0 z-50 mt-1 w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
              <div className="px-2 py-1.5 text-xs text-muted-foreground">{actor.data?.display_name ?? actor.data?.username}</div>
              <Link
                to="/admin"
                className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <SettingsIcon className="size-4" />
                平台管理
              </Link>
              <button
                type="button"
                className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => logoutMutation.mutate()}
                disabled={logoutMutation.isPending}
              >
                <LogOutIcon className="size-4" />
                退出登录
              </button>
            </div>
          </details>
        </div>
      </div>
    </header>
  )
}

export function SeverityBadge({ severity }: { severity: IncidentSeverity }) {
  return (
    <Badge variant={severity === "critical" ? "destructive" : severity === "high" ? "warning" : "outline"}>
      {severityLabels[severity]}
    </Badge>
  )
}

export function IncidentStateBadge({ state }: { state: IncidentState }) {
  const variant =
    state === "resolved"
      ? "positive"
      : state === "waiting"
        ? "warning"
        : state === "investigating"
          ? "evidence"
          : "outline"

  return <Badge variant={variant}>{stateLabels[state]}</Badge>
}

export function EvidenceRelationBadge({ relation }: { relation: EvidenceRelation }) {
  const variant =
    relation === "supports" ? "evidence" : relation === "challenges" ? "warning" : "outline"
  return <Badge variant={variant}>{relationLabels[relation]}</Badge>
}

export function EvidenceStatusBadge({ status }: { status: EvidenceStatus }) {
  const variant = status === "verified" ? "positive" : status === "partial" ? "warning" : "destructive"
  return <Badge variant={variant}>{statusLabels[status]}</Badge>
}

export function EvidenceViewButton({
  evidenceId,
  onOpen,
}: {
  evidenceId: string
  onOpen: (id: string) => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="查看证据"
            onClick={() => onOpen(evidenceId)}
          />
        }
      >
        <EyeIcon />
      </TooltipTrigger>
      <TooltipContent>查看证据</TooltipContent>
    </Tooltip>
  )
}

export function MonoValue({ className, children }: { className?: string; children: React.ReactNode }) {
  return <span className={cn("font-mono text-xs tabular-nums", className)}>{children}</span>
}
