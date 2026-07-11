import { useState } from "react"
import {
  FilePenLineIcon,
  LockKeyholeIcon,
  RotateCcwIcon,
  SendIcon,
  ServerIcon,
  ShieldAlertIcon,
  UserRoundIcon,
  XIcon,
} from "lucide-react"
import { useParams } from "react-router"

import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupText,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Separator } from "@/components/ui/separator"
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
  currentIncident,
  evidence,
  type EvidenceRelation,
} from "@/prototype/data"
import {
  ConsoleHeader,
  EvidenceDetailSheet,
  EvidenceRelationBadge,
  EvidenceStatusBadge,
  EvidenceViewButton,
  IncidentStateBadge,
  MonoValue,
  SeverityBadge,
} from "@/prototype/shared"

function IncidentMasthead() {
  return (
    <section className="border-b bg-surface" aria-labelledby="incident-title">
      <div className="mx-auto flex max-w-[1600px] flex-col gap-3 px-4 py-4 lg:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <MonoValue className="text-muted-foreground">{currentIncident.id}</MonoValue>
          <SeverityBadge severity={currentIncident.severity} />
          <IncidentStateBadge state={currentIncident.state} />
          <Badge variant="outline">已绑定资源</Badge>
        </div>

        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <h1 id="incident-title" className="text-xl font-semibold leading-7 sm:text-2xl">
              {currentIncident.title}
            </h1>
            <p className="mt-1 max-w-3xl text-sm leading-5 text-muted-foreground">
              {currentIncident.summary}
            </p>
          </div>

          <dl className="grid shrink-0 grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">目标</dt>
              <dd className="mt-0.5 font-medium">{currentIncident.service}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">环境</dt>
              <dd className="mt-0.5 font-mono text-xs">
                {currentIncident.environment} / {currentIncident.namespace}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">负责人</dt>
              <dd className="mt-0.5 font-medium">{currentIncident.owner}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">持续</dt>
              <dd className="mt-0.5"><MonoValue>{currentIncident.duration}</MonoValue></dd>
            </div>
          </dl>
        </div>
      </div>
    </section>
  )
}

type RelationFilter = "all" | EvidenceRelation
type HumanInputKind = "assertion" | "correction" | "retraction"

interface HumanInputEvent {
  id: string
  time: string
  kind: HumanInputKind
  content: string
  author: string
  referenceId?: string
}

const initialHumanInputEvents: HumanInputEvent[] = [
  {
    id: "HI-01",
    time: "09:21",
    kind: "assertion",
    content: "合作方未执行同期证书或流量操作。",
    author: "王晨",
  },
]

const humanInputLabels: Record<HumanInputKind, string> = {
  assertion: "人工输入",
  correction: "更正",
  retraction: "撤回",
}

function currentTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date())
}

function HumanContext({ onRevision }: { onRevision: () => void }) {
  const [events, setEvents] = useState(initialHumanInputEvents)
  const [draft, setDraft] = useState("")
  const [correctingId, setCorrectingId] = useState<string | null>(null)
  const [pendingRetractionId, setPendingRetractionId] = useState<string | null>(null)

  const appendEvent = (event: Omit<HumanInputEvent, "id" | "time" | "author">) => {
    setEvents((current) => [
      ...current,
      {
        ...event,
        id: `HI-${String(current.length + 1).padStart(2, "0")}`,
        time: currentTime(),
        author: "王晨",
      },
    ])
  }

  const submit = () => {
    const content = draft.trim()
    if (!content) return

    appendEvent({
      kind: correctingId ? "correction" : "assertion",
      content,
      referenceId: correctingId ?? undefined,
    })
    if (correctingId) onRevision()
    setDraft("")
    setCorrectingId(null)
  }

  const beginCorrection = (event: HumanInputEvent) => {
    setCorrectingId(event.id)
    setPendingRetractionId(null)
    setDraft(event.content)
  }

  const retract = (event: HumanInputEvent) => {
    appendEvent({
      kind: "retraction",
      content: "该项人工上下文已撤回。",
      referenceId: event.id,
    })
    setPendingRetractionId(null)
    if (correctingId === event.id) {
      setCorrectingId(null)
      setDraft("")
    }
    onRevision()
  }

  return (
    <section className="border-t" aria-labelledby="human-context-title">
      <header className="flex flex-col gap-2 p-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="human-context-title" className="text-sm font-semibold">调查记录</h2>
            <Badge variant="outline">人工上下文 · {events.length}</Badge>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            用户主张与证据分开记录；如需采信，Agent 会创建独立 Evidence Step 核实。
          </p>
        </div>
        <Badge variant="secondary">不计入 Evidence Gate</Badge>
      </header>

      <div className="divide-y border-y" role="log" aria-live="polite">
        {events.map((event) => {
          const supersedingEvent = events.find((candidate) => candidate.referenceId === event.id)
          const canRevise = event.kind !== "retraction" && !supersedingEvent

          return (
            <article
              key={event.id}
              className="grid grid-cols-[52px_minmax(0,1fr)] gap-3 px-4 py-3 sm:grid-cols-[64px_96px_minmax(0,1fr)_auto] sm:items-start"
            >
              <MonoValue className="pt-1 text-muted-foreground">{event.time}</MonoValue>
              <div className="hidden pt-0.5 sm:block">
                <Badge variant={event.kind === "retraction" ? "destructive" : "outline"}>
                  {humanInputLabels[event.kind]}
                </Badge>
              </div>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 sm:hidden">
                  <Badge variant={event.kind === "retraction" ? "destructive" : "outline"}>
                    {humanInputLabels[event.kind]}
                  </Badge>
                  <MonoValue className="text-muted-foreground">{event.id}</MonoValue>
                </div>
                <p className="mt-1 break-words text-sm leading-5 sm:mt-0">{event.content}</p>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>{event.author}</span>
                  <MonoValue>{event.id}</MonoValue>
                  {event.referenceId ? <span>引用 {event.referenceId}</span> : null}
                  {supersedingEvent ? (
                    <Badge variant="secondary">
                      {supersedingEvent.kind === "retraction" ? "已撤回" : "已被更正"}
                    </Badge>
                  ) : null}
                </div>
              </div>

              {canRevise ? (
                <div className="col-start-2 flex flex-wrap items-center justify-end gap-1 sm:col-start-4">
                  {pendingRetractionId === event.id ? (
                    <>
                      <span className="mr-1 text-xs text-muted-foreground">确认撤回？</span>
                      <Button size="xs" variant="destructive" onClick={() => retract(event)}>确认</Button>
                      <Button size="xs" variant="ghost" onClick={() => setPendingRetractionId(null)}>取消</Button>
                    </>
                  ) : (
                    <>
                      <Button size="xs" variant="ghost" onClick={() => beginCorrection(event)}>
                        <FilePenLineIcon data-icon="inline-start" />更正
                      </Button>
                      <Button size="xs" variant="ghost" onClick={() => setPendingRetractionId(event.id)}>
                        <RotateCcwIcon data-icon="inline-start" />撤回
                      </Button>
                    </>
                  )}
                </div>
              ) : null}
            </article>
          )
        })}
      </div>

      <div className="bg-surface p-4">
        <InputGroup>
          <InputGroupTextarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={correctingId ? `输入对 ${correctingId} 的更正…` : "添加调查上下文…"}
            aria-label={correctingId ? "更正人工上下文" : "添加人工上下文"}
            rows={2}
          />
          <InputGroupAddon align="block-end">
            <InputGroupText>
              {correctingId ? `原记录 ${correctingId} 将继续保留` : "不会批准或执行任何动作"}
            </InputGroupText>
            {correctingId ? (
              <InputGroupButton
                size="icon-sm"
                variant="ghost"
                aria-label="取消更正"
                onClick={() => {
                  setCorrectingId(null)
                  setDraft("")
                }}
              >
                <XIcon />
              </InputGroupButton>
            ) : null}
            <InputGroupButton
              variant="default"
              size="sm"
              className="ml-auto"
              onClick={submit}
              disabled={!draft.trim()}
            >
              <SendIcon data-icon="inline-start" />
              {correctingId ? "提交更正" : "添加到调查"}
            </InputGroupButton>
          </InputGroupAddon>
        </InputGroup>
      </div>
    </section>
  )
}

function EvidenceWorkbench({ onOpenEvidence }: { onOpenEvidence: (id: string) => void }) {
  const [relation, setRelation] = useState<RelationFilter>("all")
  const [contextRevised, setContextRevised] = useState(false)
  const visibleEvidence = relation === "all" ? evidence : evidence.filter((item) => item.relation === relation)

  return (
    <main className="mx-auto max-w-[1500px] px-4 py-5 lg:px-6">
      <div className="overflow-hidden rounded-md border lg:grid lg:grid-cols-[288px_minmax(0,1fr)]">
        <aside className="border-b bg-surface p-4 lg:border-r lg:border-b-0" aria-labelledby="case-facts-title">
          <h2 id="case-facts-title" className="text-sm font-medium">事件事实</h2>
          <dl className="mt-3 divide-y text-sm">
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">目标资源</dt>
              <dd className="mt-1 font-medium">checkout-api</dd>
              <dd className="font-mono text-[11px] text-muted-foreground">prod / payments / prod-shanghai-01</dd>
            </div>
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">责任</dt>
              <dd className="mt-1">王晨 · 支付平台</dd>
            </div>
            <div className="py-2">
              <dt className="text-xs text-muted-foreground">信号</dt>
              <dd className="mt-1">3 firing · 0 recovered</dd>
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
              {contextRevised ? <Badge variant="warning" className="mt-2">待重新评估</Badge> : null}
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

          <HumanContext onRevision={() => setContextRevised(true)} />

          <div className="border-t p-4">
            <Alert className="border-warning/30 has-data-[slot=alert-action]:pr-2.5 sm:has-data-[slot=alert-action]:pr-18">
              <ShieldAlertIcon />
              <AlertTitle>
                {contextRevised ? "建议动作已过期：等待重新评估" : "建议动作：回滚 Deployment 至 revision 41"}
              </AlertTitle>
              <AlertDescription>
                {contextRevised
                  ? "人工上下文发生更正或撤回；必须重新形成判断与 Recommended Action。"
                  : "目标与保护条件已冻结；Evidence Gate 尚缺一个实例的日志覆盖。"}
              </AlertDescription>
              <AlertAction className="static col-start-2 mt-2 justify-self-start sm:absolute sm:top-2 sm:right-2 sm:mt-0">
                <Tooltip>
                  <TooltipTrigger render={<span className="inline-block" />}>
                    <Button size="xs" disabled>
                      <LockKeyholeIcon data-icon="inline-start" />不可审批
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>
                    {contextRevised ? "先重新评估人工上下文" : "先补齐 checkout-api-7f6d9 的日志证据"}
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
  const selectedEvidence = evidence.find((item) => item.id === selectedEvidenceId) ?? null

  return (
    <div className="min-h-screen bg-background pb-8" data-prototype-incident={incidentId}>
      <ConsoleHeader showBack />
      <IncidentMasthead />
      <EvidenceWorkbench onOpenEvidence={setSelectedEvidenceId} />
      <EvidenceDetailSheet item={selectedEvidence} onClose={() => setSelectedEvidenceId(null)} />
    </div>
  )
}
