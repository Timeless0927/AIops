import { useState } from "react"
import type { LucideIcon } from "lucide-react"
import {
  ActivityIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  Clock3Icon,
  FileTextIcon,
  LockKeyholeIcon,
  ServerIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
} from "lucide-react"
import { Link, Navigate, useParams } from "react-router"

import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  evidence,
  incidents,
  type EvidenceFixture,
  type EvidenceRelation,
  type IncidentFixture,
} from "@/prototype/data"
import {
  ConsoleHeader,
  EvidenceRelationBadge,
  EvidenceStatusBadge,
  EvidenceViewButton,
  IncidentStateBadge,
  MonoValue,
  SeverityBadge,
} from "@/prototype/shared"

function FactRow({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="grid grid-cols-[20px_92px_minmax(0,1fr)] items-start gap-2 py-2 text-sm">
      <Icon className="mt-0.5 size-4 text-muted-foreground" />
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words font-medium">{value}</dd>
    </div>
  )
}

function EvidenceDetailSheet({
  item,
  onClose,
}: {
  item: EvidenceFixture | null
  onClose: () => void
}) {
  return (
    <Sheet open={Boolean(item)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-[460px]">
        {item ? (
          <>
            <SheetHeader className="border-b">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <EvidenceRelationBadge relation={item.relation} />
                <EvidenceStatusBadge status={item.status} />
              </div>
              <SheetTitle>{item.title}</SheetTitle>
              <SheetDescription>{item.summary}</SheetDescription>
            </SheetHeader>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6">
              <dl className="divide-y">
                <FactRow icon={Clock3Icon} label="时间" value={`${item.time} · ${item.phase}`} />
                <FactRow icon={ServerIcon} label="来源" value={item.source} />
                <FactRow icon={ShieldCheckIcon} label="范围" value={item.scope} />
                <FactRow icon={ActivityIcon} label="查询目的" value={item.query} />
              </dl>

              <section className="mt-5" aria-labelledby="evidence-impact-title">
                <h3 id="evidence-impact-title" className="text-sm font-medium">对当前判断的影响</h3>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">{item.impact}</p>
              </section>

              <section className="mt-5" aria-labelledby="evidence-samples-title">
                <h3 id="evidence-samples-title" className="text-sm font-medium">结构化样本</h3>
                <div className="mt-2 divide-y rounded-md border bg-surface px-3">
                  {item.samples.map((sample) => (
                    <div key={sample.label} className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 py-2.5 text-sm">
                      <span className="text-muted-foreground">{sample.label}</span>
                      <span className="break-words font-mono text-xs leading-5">{sample.value}</span>
                    </div>
                  ))}
                </div>
              </section>

              {item.status !== "verified" ? (
                <div className="mt-5 flex gap-2 rounded-md border border-warning/30 bg-warning/8 p-3 text-sm text-warning-foreground">
                  <CircleAlertIcon className="mt-0.5 size-4 shrink-0" />
                  <p>该步骤尚不能作为完整执行依据，需先补齐缺失范围。</p>
                </div>
              ) : (
                <div className="mt-5 flex gap-2 rounded-md border border-positive/30 bg-positive/8 p-3 text-sm text-positive-foreground">
                  <CheckCircle2Icon className="mt-0.5 size-4 shrink-0" />
                  <p>该证据已完成范围与引用完整性校验。</p>
                </div>
              )}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}

function IncidentMasthead({ incident }: { incident: IncidentFixture }) {
  return (
    <section className="border-b bg-surface" aria-labelledby="incident-title">
      <div className="mx-auto flex max-w-[1600px] flex-col gap-3 px-4 py-4 lg:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <MonoValue className="text-muted-foreground">{incident.id}</MonoValue>
          <SeverityBadge severity={incident.severity} />
          <IncidentStateBadge state={incident.state} />
          <Badge variant="outline">已绑定资源</Badge>
          <Link
            to={`/incidents/${incident.id}/report`}
            className="ml-auto inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            <FileTextIcon className="size-4" />事件报告
          </Link>
        </div>

        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <h1 id="incident-title" className="text-xl font-semibold leading-7 sm:text-2xl">
              {incident.title}
            </h1>
            <p className="mt-1 max-w-3xl text-sm leading-5 text-muted-foreground">
              {incident.summary}
            </p>
          </div>

          <dl className="grid shrink-0 grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">目标</dt>
              <dd className="mt-0.5 font-medium">{incident.service}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">环境</dt>
              <dd className="mt-0.5 font-mono text-xs">
                {incident.environment} / {incident.namespace}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">负责人</dt>
              <dd className="mt-0.5 font-medium">{incident.owner}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">持续</dt>
              <dd className="mt-0.5"><MonoValue>{incident.duration}</MonoValue></dd>
            </div>
          </dl>
        </div>
      </div>
    </section>
  )
}

type RelationFilter = "all" | EvidenceRelation
function EvidenceWorkbench({
  incident,
  onOpenEvidence,
}: {
  incident: IncidentFixture
  onOpenEvidence: (id: string) => void
}) {
  const [relation, setRelation] = useState<RelationFilter>("all")
  const visibleEvidence = relation === "all" ? evidence : evidence.filter((item) => item.relation === relation)

  return (
    <main className="mx-auto max-w-[1500px] px-4 py-5 lg:px-6">
      <div className="overflow-hidden rounded-md border lg:grid lg:grid-cols-[288px_minmax(0,1fr)]">
        <aside className="border-b bg-surface p-4 lg:border-r lg:border-b-0" aria-labelledby="case-facts-title">
          <h2 id="case-facts-title" className="text-sm font-medium">事件事实</h2>
          <dl className="mt-3 divide-y text-sm">
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">目标资源</dt>
              <dd className="mt-1 font-medium">{incident.service}</dd>
              <dd className="font-mono text-[11px] text-muted-foreground">
                {incident.environment} / {incident.namespace} / {incident.cluster}
              </dd>
            </div>
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">责任</dt>
              <dd className="mt-1">{incident.owner} · {incident.team}</dd>
            </div>
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">信号</dt>
              <dd className="mt-1">{incident.signalCount} 个信号</dd>
            </div>
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">Connector</dt>
              <dd className="mt-1 flex items-center gap-1.5 text-positive-foreground">
                <ServerIcon className="size-3.5" />在线 · 12 秒前
              </dd>
            </div>
          </dl>

          <Separator className="my-4" />

          <h2 className="text-sm font-medium">判断版本</h2>
          <ol className="mt-3 flex flex-col gap-3">
            <li className="rounded-md border border-evidence/30 bg-evidence/8 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium">v4 · 当前</span>
                <MonoValue>09:27</MonoValue>
              </div>
              <p className="mt-2 text-xs leading-5">TLS 配置回归导致连接复用失败。</p>
            </li>
            <li className="px-3 text-xs leading-5 text-muted-foreground">
              <div className="flex items-center justify-between gap-2"><span>v3</span><MonoValue>09:19</MonoValue></div>
              <p className="mt-1">发布变更与错误率相关。</p>
            </li>
            <li className="px-3 text-xs leading-5 text-muted-foreground">
              <div className="flex items-center justify-between gap-2"><span>v2</span><MonoValue>09:17</MonoValue></div>
              <p className="mt-1">数据库退化可能性降低。</p>
            </li>
          </ol>
        </aside>

        <section className="min-w-0" aria-labelledby="ledger-title">
          <header className="flex flex-col gap-3 border-b p-4 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h2 id="ledger-title" className="text-base font-semibold">Evidence Ledger</h2>
              <p className="mt-1 text-xs text-muted-foreground">7 个步骤 · 5 已验证 · 1 部分 · 1 缺失</p>
            </div>
            <ToggleGroup
              value={[relation]}
              onValueChange={(values) => values[0] && setRelation(values[0] as RelationFilter)}
              variant="outline"
              size="sm"
              aria-label="证据关系筛选"
            >
              <ToggleGroupItem value="all">全部</ToggleGroupItem>
              <ToggleGroupItem value="supports">支持</ToggleGroupItem>
              <ToggleGroupItem value="challenges">反证</ToggleGroupItem>
              <ToggleGroupItem value="context">上下文</ToggleGroupItem>
            </ToggleGroup>
          </header>

          <div className="hidden sm:block">
            <Table>
              <TableHeader className="bg-surface">
                <TableRow>
                  <TableHead className="w-20">时间</TableHead>
                  <TableHead>证据步骤</TableHead>
                  <TableHead className="w-32">关系</TableHead>
                  <TableHead className="w-28">状态</TableHead>
                  <TableHead className="w-36">来源</TableHead>
                  <TableHead className="w-12"><span className="sr-only">查看</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleEvidence.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell><MonoValue>{item.time}</MonoValue></TableCell>
                    <TableCell className="max-w-[520px] whitespace-normal py-3">
                      <div className="font-medium leading-5">{item.title}</div>
                      <div className="mt-1 text-xs leading-5 text-muted-foreground">{item.impact}</div>
                    </TableCell>
                    <TableCell><EvidenceRelationBadge relation={item.relation} /></TableCell>
                    <TableCell><EvidenceStatusBadge status={item.status} /></TableCell>
                    <TableCell className="max-w-36 whitespace-normal text-xs text-muted-foreground">{item.source}</TableCell>
                    <TableCell className="text-right"><EvidenceViewButton evidenceId={item.id} onOpen={onOpenEvidence} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="divide-y sm:hidden">
            {visibleEvidence.map((item) => (
              <article key={item.id} className="grid grid-cols-[48px_minmax(0,1fr)_32px] gap-3 p-3">
                <MonoValue className="pt-1 text-muted-foreground">{item.time}</MonoValue>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <EvidenceRelationBadge relation={item.relation} />
                    <EvidenceStatusBadge status={item.status} />
                  </div>
                  <h3 className="mt-2 text-sm font-medium leading-5">{item.title}</h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.impact}</p>
                  <p className="mt-2 text-xs text-muted-foreground">{item.source}</p>
                </div>
                <EvidenceViewButton evidenceId={item.id} onOpen={onOpenEvidence} />
              </article>
            ))}
          </div>

          <div className="border-t p-4">
            <Alert className="border-warning/30 has-data-[slot=alert-action]:pr-2.5 sm:has-data-[slot=alert-action]:pr-18">
              <ShieldAlertIcon />
              <AlertTitle>建议动作：回滚 Deployment 至 revision 41</AlertTitle>
              <AlertDescription>目标与保护条件已冻结；Evidence Gate 尚缺一个实例的日志覆盖。</AlertDescription>
              <AlertAction className="static col-start-2 mt-2 justify-self-start sm:absolute sm:top-2 sm:right-2 sm:mt-0">
                <Tooltip>
                  <TooltipTrigger render={<span className="inline-block" />}>
                    <Button size="xs" disabled>
                      <LockKeyholeIcon data-icon="inline-start" />不可审批
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>
                    先补齐 checkout-api-7f6d9 的日志证据
                  </TooltipContent>
                </Tooltip>
              </AlertAction>
            </Alert>
          </div>
        </section>
      </div>
    </main>
  )
}

export function WorkbenchPrototypePage() {
  const { incidentId } = useParams()
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const incident = incidents.find((item) => item.id === incidentId)
  const selectedEvidence = evidence.find((item) => item.id === selectedEvidenceId) ?? null

  if (!incident) return <Navigate to="/incidents" replace />

  return (
    <div className="min-h-screen bg-background pb-8">
      <ConsoleHeader showBack />
      <IncidentMasthead incident={incident} />
      <EvidenceWorkbench incident={incident} onOpenEvidence={setSelectedEvidenceId} />
      <EvidenceDetailSheet item={selectedEvidence} onClose={() => setSelectedEvidenceId(null)} />
    </div>
  )
}
