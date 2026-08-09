import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  BellRingIcon,
  BotIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  CircleAlertIcon,
  CloudCogIcon,
  GaugeIcon,
  RefreshCwIcon,
  SettingsIcon,
  SkipForwardIcon,
} from "lucide-react"
import { Link } from "react-router"

import {
  type CapabilityStatus,
  getConnectorAdminState,
  getNotificationDestinations,
  getPlatformStatus,
  mutateAdmin,
  setNotificationSetupDecision,
  testModelProvider,
  testNotificationDestination,
  type PlatformStatus,
} from "@/api/client"
import { ApiError } from "@/api/transport"
import type { Actor } from "@/auth/auth-client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"


type CapabilityId = keyof PlatformStatus["capabilities"]
type Mutation =
  | {kind: "verify"; capability: Exclude<CapabilityId, "observability">}
  | {kind: "decision"; decision: "active" | "skipped"}

const meta = {
  model: {
    name: "模型提供方", summary: "生成诊断判断与建议", icon: BotIcon,
    managementPath: "/admin?section=model",
  },
  notification: {
    name: "通知目的地", summary: "将事件状态送达值班人员", icon: BellRingIcon,
    managementPath: "/admin?section=notifications",
  },
  connector: {
    name: "Connector / Cluster", summary: "读取证据并执行已批准变更", icon: CloudCogIcon,
    managementPath: "/admin?section=connectors",
  },
  observability: {
    name: "可观测性", summary: "提供 Prometheus 与 Loki 证据", icon: GaugeIcon,
    managementPath: null,
  },
} as const

const capabilityIds = Object.keys(meta) as CapabilityId[]
const reasonLabels: Record<string, string> = {
  not_configured: "尚未配置",
  test_required: "需要完成真实验证",
  configuration_changed: "配置已变更，需要重新验证",
  authentication_failed: "鉴权失败",
  rate_limited: "上游限流",
  timeout: "验证超时",
  provider_unavailable: "提供方不可用",
  provider_rejected: "配置被提供方拒绝",
  invalid_response: "上游响应无效",
  owner_unavailable: "能力 owner 暂不可用",
  connector_degraded: "部分 Connector 连接降级",
  connector_disabled: "Connector Enrollment 已停用",
  connector_offline: "Connector 当前离线",
  connector_pending_registration: "Connector 等待注册",
  connector_rotation_pending: "Connector 凭据轮换中",
}

export function PlatformStatusPage({actor}: {actor: Actor}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<CapabilityId>("model")
  const [reason, setReason] = useState("")
  const status = useQuery({
    queryKey: ["platform-status"],
    queryFn: getPlatformStatus,
    retry: false,
    refetchInterval: (query) => Object.values(query.state.data?.capabilities ?? {}).some(
      (capability) => capability.verification.state === "verifying",
    ) ? 1000 : false,
  })
  const mutation = useMutation({
    mutationFn: async (action: Mutation) => {
      if (!status.data) throw new Error("Platform Status 尚未加载")
      if (action.kind === "decision") {
        const capability = status.data.capabilities.notification
        return setNotificationSetupDecision(
          action.decision, capability.configuration_revision, reason,
        )
      }
      if (action.capability === "model") {
        const revision = status.data.capabilities.model.configuration_revision
        if (!revision) throw new Error("请先配置模型提供方")
        return testModelProvider(revision, reason)
      }
      if (action.capability === "notification") {
        const {destinations} = await getNotificationDestinations()
        const destination = destinations.find((item) => item.pilot_route_selected) ?? destinations[0]
        if (!destination) throw new Error("请先配置通知目的地")
        return testNotificationDestination(destination.id, destination.configuration_revision, reason)
      }
      const connector = await getConnectorAdminState()
      const enrollment = connector.connector_enrollments.find(
        (item) => item.read_verification === "failed",
      )
      if (!enrollment) throw new Error("请在平台管理完成 Connector 注册或等待当前验证")
      return mutateAdmin({
        resource: "connector-enrollments",
        id: enrollment.id,
        body: {retry_read_verification: true, reason},
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({queryKey: ["platform-status"]})
      queryClient.invalidateQueries({queryKey: ["connectors"]})
      queryClient.invalidateQueries({queryKey: ["notification-destinations"]})
      queryClient.invalidateQueries({queryKey: ["model-provider"]})
    },
  })

  if (status.isPending) {
    return <main className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground" role="status">正在读取平台状态</main>
  }
  if (status.isError) {
    return <main className="grid min-h-[60vh] place-items-center text-sm text-destructive">平台状态暂不可用</main>
  }
  const error = mutation.error instanceof ApiError || mutation.error instanceof Error
    ? mutation.error.message : null
  return (
    <PlatformStatusView
      data={status.data}
      selected={selected}
      canAdminister={actor.is_platform_administrator}
      reason={reason}
      pending={mutation.isPending || status.isFetching}
      error={error}
      onSelect={setSelected}
      onReason={setReason}
      onRefresh={() => status.refetch()}
      onVerify={(capability) => capability === "observability"
        ? status.refetch()
        : mutation.mutate({kind: "verify", capability})}
      onSetupDecision={(decision) => mutation.mutate({kind: "decision", decision})}
    />
  )
}

export function PlatformStatusView({
  data, selected, canAdminister, reason, pending, error, onSelect, onReason = () => {},
  onRefresh, onVerify, onSetupDecision,
}: {
  data: PlatformStatus
  selected: CapabilityId
  canAdminister: boolean
  reason: string
  pending: boolean
  error: string | null
  onSelect: (capability: CapabilityId) => void
  onReason?: (reason: string) => void
  onRefresh: () => void
  onVerify: (capability: CapabilityId) => void
  onSetupDecision: (decision: "active" | "skipped") => void
}) {
  const capability = data.capabilities[selected]
  const selectedMeta = meta[selected]
  const Icon = selectedMeta.icon
  return (
    <main className="mx-auto w-full max-w-[1320px] min-w-0 overflow-x-hidden px-4 py-6 lg:px-6 lg:py-8">
      <header className="flex min-w-0 flex-wrap items-start justify-between gap-4 border-b pb-5">
        <div className="min-w-0">
          <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Platform control plane</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">平台状态</h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">查看每项能力自己的配置、验证与当前可用性；这里没有一次性的“全部完成”状态。</p>
        </div>
        <div className="flex items-center gap-2">
          {!canAdminister ? <Badge variant="outline">只读安全摘要</Badge> : null}
          <Button type="button" size="sm" variant="outline" disabled={pending} onClick={onRefresh} aria-label="刷新平台状态">
            <RefreshCwIcon />刷新
          </Button>
        </div>
      </header>

      <nav aria-label="平台能力" className="mt-5 overflow-x-auto rounded-md border bg-muted/20 p-1">
        <div className="grid min-w-[760px] grid-cols-4 gap-1">
          {capabilityIds.map((id) => {
            const item = data.capabilities[id]
            const itemMeta = meta[id]
            const ItemIcon = itemMeta.icon
            return (
              <button
                key={id}
                type="button"
                aria-current={selected === id ? "true" : undefined}
                onClick={() => onSelect(id)}
                className={cn(
                  "group min-w-[180px] rounded-sm px-3 py-3 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                  selected === id ? "bg-background shadow-xs" : "hover:bg-background/70",
                )}
              >
                <span className="flex items-center gap-2 text-sm font-medium"><ItemIcon className="size-4 text-muted-foreground" />{itemMeta.name}</span>
                <span className={cn(
                  "mt-1.5 flex items-center gap-1.5 text-xs",
                  item.readiness === "ready" ? "text-positive-foreground" : item.readiness === "skipped" ? "text-muted-foreground" : "text-destructive",
                )}>
                  <span className="size-1.5 rounded-full bg-current" />{readinessLabel(item.readiness)}
                </span>
              </button>
            )
          })}
        </div>
      </nav>

      {error ? <Alert variant="destructive" className="mt-5"><CircleAlertIcon /><AlertTitle>操作未完成</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

      <section aria-labelledby="capability-title" className="mt-5 grid min-w-0 gap-6 border-y py-6 lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.65fr)]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={capability.readiness === "ready" ? "positive" : capability.readiness === "skipped" ? "secondary" : "destructive"}>{readinessLabel(capability.readiness)}</Badge>
            <Availability value={capability.availability.state} />
          </div>
          <div className="mt-4 flex items-start gap-3">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-md border bg-muted/30"><Icon className="size-4" /></span>
            <div className="min-w-0">
              <h2 id="capability-title" className="text-xl font-semibold">{selectedMeta.name}</h2>
              <p className="mt-1 text-sm text-muted-foreground">{selectedMeta.summary}</p>
            </div>
          </div>
          <dl className="mt-6 grid gap-px overflow-hidden rounded-md border bg-border sm:grid-cols-3">
            <Fact label="配置" value={capability.configuration === "present" ? "已配置" : "未配置"} />
            <Fact label="验证" value={verificationLabel(capability.verification.state)} />
            <Fact label="最近观察" value={formatTime(capability.availability.observed_at ?? capability.verification.checked_at)} />
          </dl>
          {selected === "connector" && capability.connection ? (
            <ConnectionSummary connection={capability.connection} />
          ) : null}
          {safeReason(capability) ? (
            <div className="mt-4 flex items-start gap-2 text-sm text-muted-foreground"><CircleAlertIcon className="mt-0.5 size-4 shrink-0" /><span>{safeReason(capability)}</span></div>
          ) : (
            <div className="mt-4 flex items-start gap-2 text-sm text-positive-foreground"><CheckCircle2Icon className="mt-0.5 size-4 shrink-0" /><span>当前 owner 状态满足此能力的使用条件。</span></div>
          )}
        </div>

        <aside className="min-w-0 border-t pt-5 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
          {canAdminister ? (
            <div className="space-y-4">
              <div><h3 className="text-sm font-semibold">恢复与验证</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">配置仍由对应领域管理；这里调用真实 owner 验证或回到详细管理。</p></div>
              <Field><FieldLabel htmlFor="platform-action-reason">操作原因</FieldLabel><Input id="platform-action-reason" value={reason} onChange={(event) => onReason(event.target.value)} placeholder="说明本次配置或验证目的" /></Field>
              <div className="flex flex-wrap gap-2">
                {selectedMeta.managementPath ? <Link to={selectedMeta.managementPath} className={buttonVariants({variant: "outline", size: "sm"})}><SettingsIcon />管理详细配置</Link> : null}
                <Button type="button" size="sm" disabled={pending || (selected !== "observability" && !reason.trim())} onClick={() => onVerify(selected)}><RefreshCwIcon />验证 / 重试</Button>
                {selected === "notification" && (
                  capability.setup_decision === "skipped" || capability.readiness !== "ready"
                ) ? (
                  <Button type="button" size="sm" variant="ghost" disabled={pending || !reason.trim()} onClick={() => onSetupDecision(capability.setup_decision === "skipped" ? "active" : "skipped")}>
                    <SkipForwardIcon />{capability.setup_decision === "skipped" ? "恢复配置" : "暂时跳过"}
                  </Button>
                ) : null}
              </div>
            </div>
          ) : (
            <div><h3 className="text-sm font-semibold">只读安全摘要</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">配置地址、收件人、凭据元数据和上游原始错误仅对 owner 管理边界可见。</p></div>
          )}
        </aside>
      </section>

      <footer className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div><p className="text-sm font-medium">事件工作区始终可进入</p><p className="mt-1 text-xs text-muted-foreground">未就绪能力只会在依赖它的证据、诊断或通知动作处显示限制。</p></div>
        <Link to="/incidents" className={buttonVariants({size: "sm"})}>进入事件工作区<ChevronRightIcon /></Link>
      </footer>
    </main>
  )
}

function Fact({label, value}: {label: string; value: string}) {
  return <div className="min-w-0 bg-background px-4 py-3"><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="mt-1 truncate text-sm font-medium">{value}</dd></div>
}

function Availability({value}: {value: CapabilityStatus["availability"]["state"]}) {
  const labels = {available: "可用", degraded: "降级", unavailable: "不可用"}
  return <span className={cn("text-sm font-medium", value === "available" ? "text-positive-foreground" : value === "degraded" ? "text-warning-foreground" : "text-destructive")}>{labels[value]}</span>
}

function ConnectionSummary({connection}: {connection: NonNullable<CapabilityStatus["connection"]>}) {
  const labels = {
    pending_registration: "等待注册",
    online: "在线",
    offline: "离线",
    rotation_pending: "凭据轮换中",
    disabled: "已停用",
    degraded: "连接降级",
  }
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{connection.online} / {connection.total} 在线</span>
      {connection.states.map((state) => <Badge key={state} variant="outline">{labels[state]}</Badge>)}
    </div>
  )
}

function readinessLabel(value: CapabilityStatus["readiness"]) {
  return value === "ready" ? "就绪" : value === "skipped" ? "已跳过 · 未就绪" : "未就绪"
}

function verificationLabel(value: CapabilityStatus["verification"]["state"]) {
  return ({not_applicable: "不适用", unverified: "未验证", verifying: "验证中", verified: "已验证", failed: "验证失败", stale: "配置已变更"})[value]
}

function safeReason(capability: CapabilityStatus) {
  const code = capability.availability.reason_code ?? capability.verification.reason_code
  return code ? reasonLabels[code] ?? "能力暂不可用，请稍后重试或联系平台管理员" : null
}

function formatTime(value: number | null) {
  return value === null ? "尚无记录" : new Date(value * 1000).toLocaleString("zh-CN", {hour12: false})
}
