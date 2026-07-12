// Throwaway prototype for ticket 07. This models interaction, not backend contracts.
import {
  ActivityIcon,
  BellRingIcon,
  BotIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  CircleAlertIcon,
  CircleDashedIcon,
  CloudCogIcon,
  ExternalLinkIcon,
  GaugeIcon,
  MenuIcon,
  RefreshCwIcon,
  ServerCogIcon,
  SettingsIcon,
  ShieldCheckIcon,
  SkipForwardIcon,
  TriangleAlertIcon,
  UserRoundIcon,
  XIcon,
} from "lucide-react"
import { useMemo, useState } from "react"
import { Link, useSearchParams } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type Variant = "rail" | "queue" | "matrix"
type Scenario = "partial" | "ready" | "failed" | "readonly"
type CapabilityId = "model" | "notification" | "connector" | "observability"
type Readiness = "ready" | "not_ready" | "skipped"
type Health = "healthy" | "degraded" | "unavailable" | "unknown"

type Capability = {
  id: CapabilityId
  name: string
  shortName: string
  summary: string
  readiness: Readiness
  configuration: string
  verification: string
  availability: Health
  reason?: string
  reasonLabel?: string
  lastChecked: string
  required: boolean
}

const capabilityMeta = {
  model: {name: "模型提供方", shortName: "模型", summary: "生成诊断判断与建议", icon: BotIcon, required: true},
  notification: {name: "通知目的地", shortName: "通知", summary: "将事件状态送达值班人员", icon: BellRingIcon, required: false},
  connector: {name: "Connector / Cluster", shortName: "集群", summary: "读取证据并执行已批准变更", icon: CloudCogIcon, required: true},
  observability: {name: "可观测性", shortName: "观测", summary: "提供 Prometheus 与 Loki 证据", icon: GaugeIcon, required: true},
} as const

const scenarios: Record<Scenario, Record<CapabilityId, Omit<Capability, "id" | "name" | "shortName" | "summary" | "required">>> = {
  partial: {
    model: {readiness: "ready", configuration: "已配置 · openai-compatible", verification: "已通过 · 1 小时前", availability: "healthy", lastChecked: "12:41"},
    notification: {readiness: "skipped", configuration: "未配置", verification: "未运行", availability: "unknown", reason: "DESTINATION_NOT_CONFIGURED", reasonLabel: "尚未配置目的地", lastChecked: "-"},
    connector: {readiness: "ready", configuration: "已注册 · prod-cluster", verification: "心跳正常 · 35 秒前", availability: "healthy", lastChecked: "12:43"},
    observability: {readiness: "not_ready", configuration: "已部署 · bundled", verification: "Loki 验证失败", availability: "degraded", reason: "LOKI_QUERY_FAILED", reasonLabel: "Loki 查询未返回预期日志", lastChecked: "12:42"},
  },
  ready: {
    model: {readiness: "ready", configuration: "已配置 · openai-compatible", verification: "已通过 · 4 分钟前", availability: "healthy", lastChecked: "12:39"},
    notification: {readiness: "ready", configuration: "已配置 · sre-oncall", verification: "测试消息已确认", availability: "healthy", lastChecked: "12:38"},
    connector: {readiness: "ready", configuration: "已注册 · prod-cluster", verification: "心跳正常 · 35 秒前", availability: "healthy", lastChecked: "12:43"},
    observability: {readiness: "ready", configuration: "已部署 · bundled", verification: "Prometheus / Loki 已通过", availability: "healthy", lastChecked: "12:42"},
  },
  failed: {
    model: {readiness: "not_ready", configuration: "已配置 · openai-compatible", verification: "鉴权失败", availability: "unavailable", reason: "PROVIDER_AUTH_FAILED", reasonLabel: "模型凭据已被拒绝", lastChecked: "12:41"},
    notification: {readiness: "not_ready", configuration: "已配置 · sre-oncall", verification: "投递失败", availability: "degraded", reason: "DESTINATION_DELIVERY_FAILED", reasonLabel: "测试消息未送达", lastChecked: "12:40"},
    connector: {readiness: "ready", configuration: "已注册 · prod-cluster", verification: "心跳正常 · 35 秒前", availability: "healthy", lastChecked: "12:43"},
    observability: {readiness: "not_ready", configuration: "已部署 · bundled", verification: "Prometheus 不可用", availability: "unavailable", reason: "PROMETHEUS_UNREACHABLE", reasonLabel: "无法连接 Prometheus", lastChecked: "12:42"},
  },
  readonly: {
    model: {readiness: "ready", configuration: "已配置", verification: "最近验证通过", availability: "healthy", lastChecked: "12:41"},
    notification: {readiness: "skipped", configuration: "未启用", verification: "未运行", availability: "unknown", reason: "DESTINATION_NOT_CONFIGURED", reasonLabel: "管理员尚未配置通知", lastChecked: "-"},
    connector: {readiness: "ready", configuration: "1 个集群在线", verification: "心跳正常", availability: "healthy", lastChecked: "12:43"},
    observability: {readiness: "not_ready", configuration: "已部署", verification: "日志查询失败", availability: "degraded", reason: "LOKI_QUERY_FAILED", reasonLabel: "日志能力暂不可用", lastChecked: "12:42"},
  },
}

const variantLabels: Record<Variant, string> = {rail: "能力轨", queue: "恢复队列", matrix: "状态矩阵"}
const scenarioLabels: Record<Scenario, string> = {partial: "部分就绪", ready: "全部就绪", failed: "多项故障", readonly: "普通用户"}

function capabilitiesFor(scenario: Scenario): Capability[] {
  return (Object.keys(capabilityMeta) as CapabilityId[]).map((id) => ({id, ...capabilityMeta[id], ...scenarios[scenario][id]}))
}

function ReadinessBadge({value}: {value: Readiness}) {
  const content = value === "ready" ? "就绪" : value === "skipped" ? "已跳过 · 未就绪" : "未就绪"
  return <Badge variant={value === "ready" ? "positive" : value === "skipped" ? "secondary" : "destructive"}>{content}</Badge>
}

function HealthValue({value}: {value: Health}) {
  const labels = {healthy: "正常", degraded: "降级", unavailable: "不可用", unknown: "未知"}
  const color = value === "healthy" ? "text-positive-foreground" : value === "degraded" ? "text-warning-foreground" : value === "unknown" ? "text-muted-foreground" : "text-destructive"
  return <span className={cn("inline-flex items-center gap-1.5 text-sm font-medium", color)}><span className="size-1.5 rounded-full bg-current" />{labels[value]}</span>
}

function PrototypeHeader({scenario}: {scenario: Scenario}) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const readonly = scenario === "readonly"
  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex h-13 max-w-[1600px] items-center gap-3 px-4 lg:px-6">
        <Link to="/prototype/setup-status" className="flex min-w-0 items-center gap-2.5 text-sm font-medium">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-background"><ActivityIcon className="size-4" /></span>
          <span className="hidden sm:inline">AIOps Control Plane</span><span className="font-mono text-[11px] sm:hidden">AIOps</span>
        </Link>
        <nav aria-label="主导航" className="ml-3 hidden h-full items-center md:flex">
          <a href="#incidents" className="px-3 py-4 text-sm text-muted-foreground hover:text-foreground">事件</a>
          <a href="#platform" className="border-b-2 border-foreground px-3 py-4 text-sm font-medium">平台状态</a>
        </nav>
        <div className="ml-auto hidden items-center gap-2 sm:flex">
          <Badge variant={readonly ? "outline" : "secondary"}>{readonly ? "只读用户" : "平台管理员"}</Badge>
          <Button variant="ghost" size="icon-sm" aria-label="用户菜单"><UserRoundIcon /></Button>
        </div>
        <Button variant="ghost" size="icon-sm" className="ml-auto md:hidden" aria-label={mobileOpen ? "关闭导航" : "打开导航"} onClick={() => setMobileOpen((value) => !value)}>{mobileOpen ? <XIcon /> : <MenuIcon />}</Button>
      </div>
      {mobileOpen ? <nav className="grid border-t px-4 py-2 text-sm md:hidden"><a href="#incidents" className="py-2 text-muted-foreground">事件</a><a href="#platform" className="py-2 font-medium">平台状态</a></nav> : null}
    </header>
  )
}

function SummaryStrip({capabilities}: {capabilities: Capability[]}) {
  const ready = capabilities.filter((item) => item.readiness === "ready").length
  const unavailable = capabilities.filter((item) => item.availability === "unavailable").length
  return (
    <div className="grid border-y bg-card sm:grid-cols-3">
      <div className="flex items-center gap-3 px-4 py-3 lg:px-5"><span className="flex size-8 items-center justify-center rounded-md bg-positive/12 text-positive-foreground"><CheckCircle2Icon className="size-4" /></span><div><div className="text-lg font-semibold tabular-nums">{ready}/{capabilities.length}</div><div className="text-xs text-muted-foreground">能力就绪</div></div></div>
      <div className="flex items-center gap-3 border-t px-4 py-3 sm:border-t-0 sm:border-l lg:px-5"><span className="flex size-8 items-center justify-center rounded-md bg-warning/15 text-warning-foreground"><TriangleAlertIcon className="size-4" /></span><div><div className="text-sm font-semibold">{unavailable ? `${unavailable} 项不可用` : ready === capabilities.length ? "依赖正常" : "存在能力缺口"}</div><div className="text-xs text-muted-foreground">最近检查 12:43</div></div></div>
      <div className="flex items-center border-t px-4 py-3 text-xs text-muted-foreground sm:border-t-0 sm:border-l lg:px-5">事件工作区始终可用。缺失依赖会限制诊断或通知，不代表平台健康。</div>
    </div>
  )
}

type CapabilityChange = "configure" | "verify" | "skip"

function CapabilityActions({item, readonly, onChange}: {item: Capability; readonly: boolean; onChange: (id: CapabilityId, change: CapabilityChange) => void}) {
  if (readonly) return <p className="text-xs text-muted-foreground">只有平台管理员可以配置或验证此能力。</p>
  return (
    <div className="flex flex-wrap gap-2">
      <Button size="sm" onClick={() => onChange(item.id, "verify")}><RefreshCwIcon data-icon="inline-start" />{item.readiness === "not_ready" ? "重试验证" : "立即验证"}</Button>
      <Button size="sm" variant="outline" onClick={() => onChange(item.id, "configure")}><SettingsIcon data-icon="inline-start" />配置</Button>
      {!item.required && item.readiness !== "skipped" ? <Button size="sm" variant="ghost" onClick={() => onChange(item.id, "skip")}><SkipForwardIcon data-icon="inline-start" />暂时跳过</Button> : null}
      <Button size="sm" variant="ghost">详细管理<ExternalLinkIcon data-icon="inline-end" /></Button>
    </div>
  )
}

function RailView({items, selected, readonly, onSelect, onChange}: {items: Capability[]; selected: CapabilityId; readonly: boolean; onSelect: (id: CapabilityId) => void; onChange: (id: CapabilityId, change: CapabilityChange) => void}) {
  const item = items.find((entry) => entry.id === selected) ?? items[0]
  return (
    <div className="grid min-h-[460px] overflow-hidden rounded-md border bg-card md:grid-cols-[240px_minmax(0,1fr)]">
      <div className="border-b bg-muted/35 md:border-r md:border-b-0">
        <div className="border-b px-4 py-3 text-xs font-medium text-muted-foreground">平台能力</div>
        <div className="flex overflow-x-auto p-2 md:grid md:overflow-visible" role="list">
          {items.map((entry) => { const Icon = capabilityMeta[entry.id].icon; return <button key={entry.id} type="button" onClick={() => onSelect(entry.id)} className={cn("flex min-w-[180px] items-center gap-3 rounded-md px-3 py-3 text-left hover:bg-background md:min-w-0", selected === entry.id && "bg-background shadow-xs")}><Icon className="size-4 shrink-0 text-muted-foreground" /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{entry.name}</span><span className={cn("block text-xs", entry.readiness === "ready" ? "text-positive-foreground" : entry.readiness === "skipped" ? "text-muted-foreground" : "text-destructive")}>{entry.readiness === "ready" ? "就绪" : entry.readiness === "skipped" ? "已跳过 · 未就绪" : "需要处理"}</span></span><ChevronRightIcon className="size-3.5 text-muted-foreground" /></button>})}
        </div>
      </div>
      <section className="min-w-0 p-4 md:p-6" aria-labelledby="capability-title">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b pb-5"><div><div className="mb-2 flex items-center gap-2"><ReadinessBadge value={item.readiness} /><HealthValue value={item.availability} /></div><h2 id="capability-title" className="text-xl font-semibold">{item.name}</h2><p className="mt-1 text-sm text-muted-foreground">{item.summary}</p></div><CapabilityActions item={item} readonly={readonly} onChange={onChange} /></div>
        {item.reason ? <div className="mt-5 flex gap-3 rounded-md border border-warning/35 bg-warning/8 p-3"><CircleAlertIcon className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><div><div className="text-sm font-medium">{item.reasonLabel}</div><div className="mt-1 font-mono text-xs text-muted-foreground">{item.reason}</div></div></div> : null}
        <dl className="mt-6 grid gap-0 rounded-md border sm:grid-cols-2"><div className="border-b p-4 sm:border-r"><dt className="text-xs text-muted-foreground">配置</dt><dd className="mt-1 text-sm font-medium">{item.configuration}</dd></div><div className="border-b p-4"><dt className="text-xs text-muted-foreground">验证</dt><dd className="mt-1 text-sm font-medium">{item.verification}</dd></div><div className="p-4 sm:border-r"><dt className="text-xs text-muted-foreground">运行可用性</dt><dd className="mt-1"><HealthValue value={item.availability} /></dd></div><div className="p-4"><dt className="text-xs text-muted-foreground">最近检查</dt><dd className="mt-1 font-mono text-sm">{item.lastChecked}</dd></div></dl>
      </section>
    </div>
  )
}

function QueueView({items, readonly, onChange}: {items: Capability[]; readonly: boolean; onChange: (id: CapabilityId, change: CapabilityChange) => void}) {
  const ordered = [...items].sort((a, b) => (a.readiness === "ready" ? 1 : 0) - (b.readiness === "ready" ? 1 : 0))
  return <div className="overflow-hidden rounded-md border bg-card"><div className="flex items-center justify-between border-b px-4 py-3"><div><h2 className="font-semibold">恢复队列</h2><p className="text-xs text-muted-foreground">按影响排序，可随时离开并从当前状态继续</p></div><Badge variant="outline">{ordered.filter((item) => item.readiness !== "ready").length} 项待处理</Badge></div><ol className="divide-y">{ordered.map((item, index) => { const Icon = capabilityMeta[item.id].icon; return <li key={item.id} className="grid gap-3 p-4 lg:grid-cols-[32px_minmax(180px,0.7fr)_minmax(260px,1fr)_auto] lg:items-center"><span className="flex size-7 items-center justify-center rounded-md border font-mono text-xs">{index + 1}</span><div className="flex min-w-0 items-center gap-3"><Icon className="size-4 shrink-0 text-muted-foreground" /><div className="min-w-0"><div className="truncate text-sm font-medium">{item.name}</div><div className="text-xs text-muted-foreground">{item.summary}</div></div></div><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><ReadinessBadge value={item.readiness} /><HealthValue value={item.availability} /></div><div className="mt-1 truncate text-xs text-muted-foreground">{item.reasonLabel ?? item.verification}</div></div><CapabilityActions item={item} readonly={readonly} onChange={onChange} /></li>})}</ol></div>
}

function MatrixView({items, readonly, onChange}: {items: Capability[]; readonly: boolean; onChange: (id: CapabilityId, change: CapabilityChange) => void}) {
  return <div className="overflow-x-auto rounded-md border bg-card"><table className="w-full min-w-[820px] text-left text-sm"><thead className="border-b bg-muted/40 text-xs text-muted-foreground"><tr><th className="px-4 py-3 font-medium">能力</th><th className="px-4 py-3 font-medium">Readiness</th><th className="px-4 py-3 font-medium">配置</th><th className="px-4 py-3 font-medium">验证</th><th className="px-4 py-3 font-medium">Availability</th><th className="px-4 py-3 font-medium">最近检查</th><th className="px-4 py-3 font-medium">操作</th></tr></thead><tbody className="divide-y">{items.map((item) => { const Icon = capabilityMeta[item.id].icon; return <tr key={item.id} className="align-top hover:bg-muted/20"><td className="px-4 py-3"><div className="flex items-center gap-2 font-medium"><Icon className="size-4 text-muted-foreground" />{item.name}</div>{item.reason ? <div className="mt-1 max-w-56 font-mono text-[11px] text-destructive">{item.reason}</div> : null}</td><td className="px-4 py-3"><ReadinessBadge value={item.readiness} /></td><td className="max-w-52 px-4 py-3 text-muted-foreground">{item.configuration}</td><td className="max-w-52 px-4 py-3 text-muted-foreground">{item.verification}</td><td className="px-4 py-3"><HealthValue value={item.availability} /></td><td className="px-4 py-3 font-mono text-xs">{item.lastChecked}</td><td className="px-4 py-3">{readonly ? <span className="text-xs text-muted-foreground">只读</span> : <Button size="sm" variant="outline" onClick={() => onChange(item.id, "verify")}><RefreshCwIcon />验证</Button>}</td></tr>})}</tbody></table></div>
}

function PrototypeControls({variant, scenario, onVariant, onScenario}: {variant: Variant; scenario: Scenario; onVariant: (value: Variant) => void; onScenario: (value: Scenario) => void}) {
  return <div className="mx-4 mt-3 flex w-[calc(100%-2rem)] max-w-fit flex-col gap-2 rounded-md border bg-popover p-2 shadow-sm sm:mx-auto sm:w-[calc(100%-1rem)] sm:flex-row sm:items-center"><span className="hidden px-1 text-[11px] font-medium text-muted-foreground lg:inline">原型视图</span><ToggleGroup value={[variant]} onValueChange={(values) => values[0] && onVariant(values[0] as Variant)} variant="outline" size="sm" spacing={0} aria-label="信息架构方案">{(Object.keys(variantLabels) as Variant[]).map((value) => <ToggleGroupItem key={value} value={value}>{variantLabels[value]}</ToggleGroupItem>)}</ToggleGroup><span className="hidden h-5 border-l sm:block" /><ToggleGroup value={[scenario]} onValueChange={(values) => values[0] && onScenario(values[0] as Scenario)} variant="outline" size="sm" spacing={0} aria-label="平台状态场景">{(Object.keys(scenarioLabels) as Scenario[]).map((value) => <ToggleGroupItem key={value} value={value}>{scenarioLabels[value]}</ToggleGroupItem>)}</ToggleGroup></div>
}

export function SetupStatusPrototypePage() {
  const [params, setParams] = useSearchParams()
  const variant = (["rail", "queue", "matrix"].includes(params.get("variant") ?? "") ? params.get("variant") : "rail") as Variant
  const scenario = (["partial", "ready", "failed", "readonly"].includes(params.get("state") ?? "") ? params.get("state") : "partial") as Scenario
  const [selected, setSelected] = useState<CapabilityId>("observability")
  const [overrides, setOverrides] = useState<Partial<Record<CapabilityId, Capability>>>({})
  const items = useMemo(() => capabilitiesFor(scenario).map((item) => overrides[item.id] ?? item), [scenario, overrides])
  const readonly = scenario === "readonly"
  const updateParam = (key: string, value: string) => { const next = new URLSearchParams(params); next.set(key, value); setParams(next) }
  const change = (id: CapabilityId, action: CapabilityChange) => {
    const current = items.find((item) => item.id === id)
    if (!current) return
    const next = action === "skip"
      ? {...current, readiness: "skipped" as const, configuration: "未配置", verification: "未运行", availability: "unknown" as const, reason: "DESTINATION_NOT_CONFIGURED", reasonLabel: "已由管理员暂时跳过", lastChecked: "-"}
      : action === "configure"
        ? {...current, readiness: "not_ready" as const, configuration: "已配置 · 待验证", verification: "尚未验证", availability: "unknown" as const, reason: "VERIFICATION_REQUIRED", reasonLabel: "配置已保存，需要验证", lastChecked: "-"}
        : {...current, readiness: "ready" as const, verification: "刚刚验证通过", availability: "healthy" as const, reason: undefined, reasonLabel: undefined, lastChecked: "刚刚"}
    setOverrides((values) => ({...values, [id]: next}))
  }
  const changeScenario = (value: Scenario) => { setOverrides({}); updateParam("state", value) }

  return <div className="min-h-screen bg-background pb-8"><PrototypeHeader scenario={scenario} /><PrototypeControls variant={variant} scenario={scenario} onVariant={(value) => updateParam("variant", value)} onScenario={changeScenario} /><main id="platform" className="mx-auto max-w-[1320px] px-4 py-5 lg:px-6 lg:py-7"><div className="mb-5 flex flex-wrap items-start justify-between gap-4"><div><div className="mb-1 flex items-center gap-2 text-xs font-medium text-muted-foreground"><ServerCogIcon className="size-3.5" />平台运行面</div><h1 className="text-2xl font-semibold">平台状态</h1><p className="mt-1 max-w-2xl text-sm text-muted-foreground">检查关键能力的配置、验证与当前可用性。这里不是一次性的安装完成页。</p></div><div className="flex flex-wrap gap-2"><Button variant="outline"><ShieldCheckIcon data-icon="inline-start" />审计记录</Button>{!readonly ? <Button variant="outline"><SettingsIcon data-icon="inline-start" />打开平台管理</Button> : null}<Tooltip><TooltipTrigger render={<Button aria-label="刷新全部状态" size="icon" variant="outline" onClick={() => setOverrides({})} />}><RefreshCwIcon /></TooltipTrigger><TooltipContent>刷新全部状态</TooltipContent></Tooltip></div></div><SummaryStrip capabilities={items} /><div className="mt-5">{variant === "rail" ? <RailView items={items} selected={selected} readonly={readonly} onSelect={setSelected} onChange={change} /> : variant === "queue" ? <QueueView items={items} readonly={readonly} onChange={change} /> : <MatrixView items={items} readonly={readonly} onChange={change} />}</div><div id="incidents" className="mt-5 flex flex-col gap-3 border-t pt-5 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-start gap-3"><CircleDashedIcon className="mt-0.5 size-4 text-muted-foreground" /><div><div className="text-sm font-medium">事件工作区无需等待全部能力就绪</div><p className="text-xs text-muted-foreground">未就绪能力会在相关证据和动作处显示明确限制。</p></div></div><Button>进入事件工作区<ChevronRightIcon data-icon="inline-end" /></Button></div><div className="mt-5 flex items-start gap-3 rounded-md border bg-muted/25 p-4"><ServerCogIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><div><div className="text-sm font-medium">Platform Status 与 /admin 的分工</div><p className="mt-1 text-xs leading-5 text-muted-foreground">此页汇总跨能力 readiness 并提供恢复入口；/admin 继续负责用户、权限、集群、资源目录和通知目的地等详细管理。普通用户只能看到安全摘要。</p></div></div></main></div>
}
