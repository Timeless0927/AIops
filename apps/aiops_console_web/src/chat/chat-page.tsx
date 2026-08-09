import { useEffect, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate, useParams } from "react-router"
import { ArrowDown, ArrowRight, ArrowUp, ChevronLeft, ChevronRight, LoaderCircle, Menu, Paperclip, Pencil, PanelLeftOpen, RefreshCw, Square, X } from "lucide-react"
import {
  AssistantRuntimeProvider,
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ThreadMessage,
  useExternalStoreRuntime,
} from "@assistant-ui/react"

import {
  listResourceWorkspace,
  type ResourceWorkspace,
} from "@/api/client"
import { ApiError } from "@/api/transport"
import { reserveChatAttachment, retryChatAttachment, uploadChatAttachment, type ChatAttachment, type ChatHandoff, type ChatHandoffTarget, type ChatSession, type ChatSessionSummary } from "@/chat/chat-client"
import { chatAttachmentAdapter, chatMessageRepository, chatThreadListAdapter, textFromAssistantMessage, type ChatAttachmentAdapter, type GatewayMessageMetadata } from "@/chat/chat-runtime"
import { ChatComposerAttachments, ChatMessageAttachments } from "@/chat/chat-attachments"
import { useChatSessionController } from "@/chat/chat-session-controller"
import { ChatThreadList } from "@/chat/chat-thread-list"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Textarea } from "@/components/ui/textarea"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { listIncidents, type Incident } from "@/incidents/incident-client"

type ChatViewProps = {
  sessions: ChatSessionSummary[]
  session: ChatSession | null
  pendingContent: string | null
  connection: "connecting" | "connected" | "reconnecting"
  resources: ResourceWorkspace["resources"]
  incidents: Incident[]
  selectedTargetId: string
  busy: boolean
  generating?: boolean
  loading?: boolean
  error: string | null
  handoff: ChatHandoff | null
  actionBusy: boolean
  query: string
  filter: "all" | "normal" | "pinned" | "archived"
  attachments?: ChatAttachment[]
  attachmentBusy?: boolean
  attachmentAdapter?: ChatAttachmentAdapter
  onCreate: () => void
  onSelect: (sessionId: string) => void
  onQueryChange: (query: string) => void
  onFilterChange: (filter: "all" | "normal" | "pinned" | "archived") => void
  onRename: (sessionId: string, title: string) => void
  onPin: (sessionId: string, pinned: boolean) => void
  onArchive: (sessionId: string, archived: boolean) => void
  onDelete: (sessionId: string) => void
  onSend: (content: string, attachmentIds: string[]) => void
  onCancel?: () => void
  onRemoveAttachment?: (attachmentId: string) => void
  onRetryAttachment?: (attachmentId: string) => void
  onScopeChange: (targetId: string) => void
  onRetry: (messageId: string) => void
  onEdit: (messageId: string, content: string) => void
  onReload: (messageId: string) => void
  onSwitchBranch: (messageId: string) => void
  onHandoff: (messageIds: string[], target: ChatHandoffTarget) => void
  onDismissHandoff?: () => void
  onOpenInvestigation?: (incidentId: string) => void
}

const statusLabels: Record<string, string> = {
  accepted: "已接受",
  completed: "已完成",
  failed: "失败",
  running: "运行中",
  succeeded: "成功",
  validated: "已验证",
  knowledge_answered: "知识问答已完成",
  cancelled: "已停止",
  user_cancelled: "手动停止",
}
const statusLabel = (status: string) => statusLabels[status] ?? status
const chatErrorLabels: Record<string, string> = {
  forbidden: "无权执行此 AI 对话操作。",
  handoff_target_not_found: "目标 Incident 不存在或无权访问。",
  resource_not_bound: "所选资源未绑定到有效 Service。",
  investigation_terminal: "目标 Investigation 已结束，不能接收 Human Input。",
  chat_message_not_found: "所选 AI 对话消息不存在或尚未完成。",
  attachment_not_ready: "所选消息包含尚未通过安全检查的附件。",
  message_not_cancellable: "当前没有正在生成的回答。",
}

export function chatErrorMessage(failure: unknown): string | null {
  if (failure instanceof ApiError) {
    return chatErrorLabels[failure.code] ?? (failure.status === 404 ? "AI 对话会话不存在或无权访问。" : failure.message)
  }
  return failure ? "AI 对话暂时不可用。" : null
}

function MessageEditComposer() {
  return (
    <ComposerPrimitive.Root className="mt-3 border-t pt-2">
      <ComposerPrimitive.Input aria-label="编辑消息" maxLength={8000} className="min-h-20 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm" />
      <div className="mt-2 flex gap-2">
        <ComposerPrimitive.Send render={<Button size="sm" />}>
          发送编辑
        </ComposerPrimitive.Send>
        <ComposerPrimitive.Cancel render={<Button size="sm" variant="ghost" />}>
          取消
        </ComposerPrimitive.Cancel>
      </div>
    </ComposerPrimitive.Root>
  )
}

export function HandoffSuccessContent({
  handoff,
  onOpenInvestigation = () => undefined,
}: {
  handoff: ChatHandoff
  onOpenInvestigation?: (incidentId: string) => void
}) {
  return <>
    <DialogHeader>
      <DialogTitle>已转交事件调查</DialogTitle>
      <DialogDescription>{handoff.idempotent ? "重复请求已安全返回相同结果。" : "选定内容已作为 Human Input 复制到事件调查。"}</DialogDescription>
    </DialogHeader>
    <div className="grid gap-2 rounded-md border p-3 text-sm">
      <p className="break-all">Incident：{handoff.incident_id}</p>
      <p className="break-all">Investigation：{handoff.investigation_id}</p>
      <p className="text-muted-foreground">这些材料不会自动成为 Evidence、Approval 或执行授权。</p>
    </div>
    <DialogFooter>
      <DialogClose render={<Button type="button" variant="outline" />}>留在 AI 对话</DialogClose>
      <Button type="button" onClick={() => onOpenInvestigation(handoff.incident_id)}><ArrowRight />进入事件调查</Button>
    </DialogFooter>
  </>
}

export function ChatView({
  sessions,
  session,
  pendingContent,
  connection,
  resources,
  incidents,
  selectedTargetId,
  busy,
  generating,
  loading = false,
  error,
  handoff,
  actionBusy,
  query,
  filter,
  attachments = [],
  attachmentBusy = false,
  attachmentAdapter,
  onCreate,
  onSelect,
  onQueryChange,
  onFilterChange,
  onRename,
  onPin,
  onArchive,
  onDelete,
  onSend,
  onCancel = () => undefined,
  onRemoveAttachment = () => undefined,
  onRetryAttachment = () => undefined,
  onScopeChange,
  onRetry,
  onEdit,
  onReload,
  onSwitchBranch,
  onHandoff,
  onDismissHandoff = () => undefined,
  onOpenInvestigation = () => undefined,
}: ChatViewProps) {
  const [threadsOpen, setThreadsOpen] = useState(true)
  const [selectedMessageIds, setSelectedMessageIds] = useState<string[]>([])
  const [handoffTargetType, setHandoffTargetType] = useState<"existing_incident" | "user_created_incident">("existing_incident")
  const [incidentId, setIncidentId] = useState("")
  const [problemSummary, setProblemSummary] = useState("")
  const [handoffResourceId, setHandoffResourceId] = useState("")
  const [handoffOpen, setHandoffOpen] = useState(false)
  const selectedResource = resources.find((resource) => resource.id === selectedTargetId)
  const targetIncidentId = incidentId || incidents[0]?.id || ""
  const targetIncident = incidents.find((incident) => incident.id === targetIncidentId)
  const handoffResource = resources.find((resource) => resource.id === handoffResourceId)
  const canHandoff = selectedMessageIds.length > 0 && (handoffTargetType === "existing_incident"
    ? Boolean(targetIncidentId)
    : Boolean(problemSummary.trim() && handoffResource?.binding_state === "bound"))
  const hasSendingMessage = Boolean(session?.messages.some((message) => message.status === "sending"))
  const responseRunning = generating ?? hasSendingMessage
  const locked = busy || hasSendingMessage
  const composerAttachments = attachments.filter((attachment) => !attachment.message_id)
  const readyAttachmentIds = composerAttachments.filter((attachment) => attachment.status === "ready").map((attachment) => attachment.id)
  const attachmentPending = composerAttachments.some((attachment) => attachment.status !== "ready")
  const selectedAttachments = attachments.filter((attachment) => attachment.status === "ready" && Boolean(attachment.message_id) && selectedMessageIds.includes(attachment.message_id!))
  const repository = useMemo(() => chatMessageRepository(session), [session])
  const threadSessions = useMemo(() => session ? [session, ...sessions.filter((item) => item.id !== session.id)] : sessions, [session, sessions])
  const threadListAdapter = useMemo(() => chatThreadListAdapter({
    threadId: session?.id,
    sessions: threadSessions,
    archived: filter === "archived",
    onCreate,
    onSelect,
    onRename,
    onPin,
    onArchive,
    onDelete,
  }), [filter, onArchive, onCreate, onDelete, onPin, onRename, onSelect, session?.id, threadSessions])
  const createOrSelect = () => { void threadListAdapter.onSwitchToNewThread?.() }
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning: responseRunning,
    isDisabled: locked,
    isSendDisabled: attachmentPending,
    onNew: async (message) => {
      const content = textFromAssistantMessage(message.content)
      if (content.trim() && !attachmentPending) onSend(content, readyAttachmentIds)
    },
    onCancel: async () => onCancel(),
    onEdit: async (message) => {
      const content = textFromAssistantMessage(message.content)
      if (message.sourceId && content.trim()) onEdit(message.sourceId, content)
    },
    onReload: async (_parentId, config) => {
      if (config.sourceId) onReload(config.sourceId)
    },
    onRefetchThread: async () => undefined,
    adapters: {attachments: attachmentAdapter, threadList: threadListAdapter},
    setMessages: () => undefined,
    unstable_onBranchChange: ({headId}) => { if (headId) onSwitchBranch(headId) },
  })

  useEffect(() => {
    setSelectedMessageIds([])
    setProblemSummary("")
    setIncidentId("")
    setHandoffResourceId("")
    setHandoffOpen(false)
  }, [session?.id])

  useEffect(() => {
    if (handoff) setHandoffOpen(false)
  }, [handoff])

  function submitHandoff() {
    if (!canHandoff) return
    const target: ChatHandoffTarget = handoffTargetType === "existing_incident"
      ? {type: "existing_incident", incident_id: targetIncidentId}
      : {
          type: "user_created_incident",
          problem_summary: problemSummary.trim(),
          scope: {cluster_id: handoffResource!.cluster_id, deployment_target_id: handoffResource!.id},
        }
    onHandoff(selectedMessageIds, target)
  }
  const threadList = (showCollapse = false) => <ChatThreadList
    sessions={sessions}
    activeId={session?.id}
    busy={busy}
    actionBusy={actionBusy}
    query={query}
    filter={filter}
    onCreate={createOrSelect}
    onSelect={onSelect}
    onQueryChange={onQueryChange}
    onFilterChange={onFilterChange}
    onRename={onRename}
    onPin={onPin}
    onArchive={onArchive}
    onDelete={onDelete}
    showCollapse={showCollapse}
    onCollapse={() => setThreadsOpen(false)}
  />
  return (
    <AssistantRuntimeProvider runtime={runtime}>
    <main className={`mx-auto grid h-[calc(100dvh-3.25rem)] min-h-0 w-full max-w-[1800px] min-w-0 bg-muted/30 p-2 font-['Geist_Variable','Noto_Sans_SC_Variable',sans-serif] md:pl-0 ${threadsOpen ? "md:grid-cols-[17rem_1fr]" : "md:grid-cols-[3.5rem_1fr]"}`}>
      {threadsOpen ? <aside className="hidden min-h-0 min-w-0 md:block">{threadList(true)}</aside> : <aside className="hidden md:flex md:justify-center md:p-3">
        <Tooltip><TooltipTrigger render={<Button type="button" size="icon" variant="ghost" className="rounded-full" aria-label="展开会话栏" onClick={() => setThreadsOpen(true)} />}><PanelLeftOpen /></TooltipTrigger><TooltipContent side="right">展开会话栏</TooltipContent></Tooltip>
      </aside>}

      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-xl bg-background shadow-[0_1px_2px_rgba(0,0,0,0.04)]" aria-label="AI 对话消息">
        {session ? (
          <>
            <header className="flex h-12 shrink-0 items-center justify-between gap-3 px-4">
              <div className="flex min-w-0 items-center gap-2">
                <Sheet><SheetTrigger render={<Button type="button" size="icon-sm" variant="ghost" className="rounded-full md:hidden" aria-label="打开会话栏" />}><Menu /></SheetTrigger><SheetContent side="left" className="w-[min(22rem,90vw)] gap-0 p-0"><SheetHeader className="sr-only"><SheetTitle>AI 对话会话</SheetTitle><SheetDescription>搜索、筛选和切换 AI 对话会话。</SheetDescription></SheetHeader>{threadList()}</SheetContent></Sheet>
                <h2 className="truncate text-sm font-medium">{session.title}</h2>
              </div>
              <p className="shrink-0 text-xs text-muted-foreground" role="status" aria-live="polite">
                {connection === "connected" ? "实时更新已连接" : connection === "reconnecting" ? "连接已断开，正在恢复实时更新" : "正在连接实时更新"}
              </p>
            </header>
            <ThreadPrimitive.Root className="@container flex min-h-0 flex-1 flex-col" style={{["--thread-max-width" as string]: "44rem", ["--composer-radius" as string]: "1.5rem"}}>
            <ThreadPrimitive.Viewport turnAnchor="top" className="relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth px-4 pt-4" aria-live="polite">
              <div className="mb-14 flex flex-col gap-y-6 empty:hidden">
              <ThreadPrimitive.Messages>
              {({message: runtimeMessage}) => {
                const message = session.messages.find((candidate) => candidate.id === runtimeMessage.id)
                if (!message) return null
                const gateway = runtimeMessage.metadata.custom.gateway as GatewayMessageMetadata | undefined
                return (
                <MessagePrimitive.Root asChild>
                <article
                  data-role={message.role}
                  className="animate-in fade-in slide-in-from-bottom-1 mx-auto w-full max-w-[44rem] px-2 duration-150 motion-reduce:animate-none"
                >
                  <div className={message.role === "user" ? "ml-auto w-fit max-w-[85%] rounded-xl bg-muted px-4 py-2 text-foreground" : "px-2 leading-relaxed text-foreground"}>
                  {message.status !== "completed" ? <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                    {message.status === "sending" && !message.content ? <span className="inline-flex items-center gap-2" role="status"><LoaderCircle className="size-3.5 animate-spin motion-reduce:animate-none" />正在思考</span> : null}
                    {message.status === "failed" ? <Badge variant="destructive">回答失败</Badge> : null}
                  </div> : null}
                  <div className="break-words text-sm leading-relaxed"><MessagePrimitive.Parts />{message.status === "sending" && message.content ? <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-foreground align-text-bottom motion-reduce:animate-none" aria-hidden="true" /> : null}</div>
                  {message.status === "completed" && message.completion?.stopping_reason === "user_cancelled" && !message.content ? <p className="text-sm text-muted-foreground">已停止生成</p> : null}
                  {message.status === "failed" ? <p className="mt-2 break-words text-xs text-destructive">失败原因：{message.error_code === "model_unavailable" ? "模型服务暂时不可用" : "回答服务暂时不可用"}</p> : null}
                  <ChatMessageAttachments attachments={gateway?.attachments ?? message.attachments ?? []} />
                  {(gateway?.scope ?? message.scope)?.resources.length ? <div className="mt-3 border-t border-border/60 pt-2 text-xs">
                    <p className="font-medium">环境范围</p>
                    {(gateway?.scope ?? message.scope)!.resources.map((resource) => <p key={resource.deployment_target_id} className="mt-1 break-words text-muted-foreground">
                      {resource.cluster_id} / {resource.namespace} / {resource.workload_kind} / {resource.workload_name}
                    </p>)}
                  </div> : null}
                  {(gateway?.nextStep ?? message.next_step) ? <p className="mt-2 text-xs"><span className="font-medium">下一步：</span>{gateway?.nextStep ?? message.next_step}</p> : null}
                  {(gateway?.toolActivity ?? message.tool_activity).length || (gateway?.evidenceReferences ?? message.evidence_references).length || (gateway?.uncertainty ?? message.uncertainty) || (gateway?.completion ?? message.completion) || (gateway?.skillVersions ?? message.skill_versions).length ? <details className="mt-3 border-t border-border/60 pt-2 text-xs">
                    <summary className="w-fit cursor-pointer font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">查看分析详情</summary>
                    {(gateway?.toolActivity ?? message.tool_activity).length ? <section className="mt-2" aria-label="工具活动"><p className="font-medium">工具活动</p><ul className="mt-1 space-y-2">{(gateway?.toolActivity ?? message.tool_activity).map((activity, index) => <li key={`${activity.tool}-${index}`}><div className="flex flex-wrap items-center gap-2"><span>{activity.tool}</span><Badge variant="outline">{statusLabel(activity.status)}</Badge></div><p className="mt-1 break-words text-muted-foreground">{activity.summary}</p>{activity.missing_reason ? <p className="mt-1 break-words text-muted-foreground">{activity.missing_reason}</p> : null}</li>)}</ul></section> : null}
                    {(gateway?.evidenceReferences ?? message.evidence_references).length ? <div className="mt-2"><p className="font-medium">Evidence 引用</p><ul className="mt-1 space-y-1 text-muted-foreground">{(gateway?.evidenceReferences ?? message.evidence_references).map((reference) => <li key={reference} className="break-all">{reference}</li>)}</ul></div> : null}
                    {(gateway?.uncertainty ?? message.uncertainty) ? <p className="mt-2 text-muted-foreground">不确定性：{statusLabel((gateway?.uncertainty ?? message.uncertainty)!.status)}{(gateway?.uncertainty ?? message.uncertainty)!.reasons.length ? ` · ${(gateway?.uncertainty ?? message.uncertainty)!.reasons.join("；")}` : ""}</p> : null}
                    {(gateway?.completion ?? message.completion) ? <p className="mt-2 text-muted-foreground">完成状态：{statusLabel((gateway?.completion ?? message.completion)!.status)} · {statusLabel((gateway?.completion ?? message.completion)!.stopping_reason)}</p> : null}
                    {(gateway?.skillVersions ?? message.skill_versions).length ? <p className="mt-2 text-muted-foreground">技能版本：{(gateway?.skillVersions ?? message.skill_versions).map((skill) => `${skill.name} v${skill.version}`).join("、")}</p> : null}
                  </details> : null}
                  {message.status === "failed" ? <Button className="mt-2" size="sm" variant="outline" onClick={() => onRetry(message.id)} disabled={locked}>重试</Button> : null}
                  </div>
                  <div className={`mt-1 flex min-h-7 items-center gap-1 text-muted-foreground ${message.role === "user" ? "justify-end" : "pl-1"}`}>
                  {runtimeMessage.composer.isEditing ? <MessageEditComposer /> : <ActionBarPrimitive.Root className="flex items-center gap-1">
                    {message.role === "user" && message.status === "completed" ? <ActionBarPrimitive.Edit render={<Button size="icon-sm" variant="ghost" className="rounded-full" aria-label="编辑" />}><Pencil /></ActionBarPrimitive.Edit> : null}
                    {message.role === "assistant" && message.status === "completed" ? locked
                      ? <Button size="icon-sm" variant="ghost" className="rounded-full" aria-label="重新生成" disabled><RefreshCw /></Button>
                      : <ActionBarPrimitive.Reload render={<Button size="icon-sm" variant="ghost" className="rounded-full" aria-label="重新生成" />}><RefreshCw /></ActionBarPrimitive.Reload>
                    : null}
                  </ActionBarPrimitive.Root>}
                  {message.branch_count > 1 ? <BranchPickerPrimitive.Root className="flex items-center text-xs">
                      <BranchPickerPrimitive.Previous className="grid size-7 place-items-center rounded-full hover:bg-muted" aria-label="上一分支"><ChevronLeft className="size-4" /></BranchPickerPrimitive.Previous>
                      <span className="font-medium"><BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count /></span>
                      <BranchPickerPrimitive.Next className="grid size-7 place-items-center rounded-full hover:bg-muted" aria-label="下一分支"><ChevronRight className="size-4" /></BranchPickerPrimitive.Next>
                    </BranchPickerPrimitive.Root> : null}
                  {message.status === "completed" && message.content ? <label className="ml-1 inline-flex size-7 cursor-pointer items-center justify-center rounded-full transition-colors hover:bg-muted focus-within:ring-2 focus-within:ring-ring">
                    <Checkbox
                      aria-label={`选择消息 ${message.content}`}
                      checked={selectedMessageIds.includes(message.id)}
                      onCheckedChange={(checked) => setSelectedMessageIds((current) => checked ? [...current, message.id] : current.filter((id) => id !== message.id))}
                    />
                    <span className="sr-only">选择此消息用于 Handoff</span>
                  </label> : null}
                  </div>
                </article>
                </MessagePrimitive.Root>
                )
              }}
              </ThreadPrimitive.Messages>
              {pendingContent && !session.messages.some((message) => message.role === "user" && message.content === pendingContent) ? <article className="mx-auto w-full max-w-[44rem] px-2"><div className="ml-auto w-fit max-w-[85%] rounded-xl bg-muted px-4 py-2 text-foreground"><p className="whitespace-pre-wrap break-words text-sm">{pendingContent}</p></div></article> : null}
              {pendingContent && !hasSendingMessage ? <article className="mx-auto w-full max-w-[44rem] px-4 text-sm text-muted-foreground" role="status"><span className="inline-flex items-center gap-2"><LoaderCircle className="size-3.5 animate-spin motion-reduce:animate-none" />正在思考</span></article> : null}
              </div>

              <ThreadPrimitive.ViewportFooter className="relative sticky bottom-0 mt-auto mx-auto flex w-full max-w-[44rem] flex-col gap-3 rounded-t-[1.5rem] bg-background/95 pb-4 backdrop-blur-sm md:pb-6">
                <ThreadPrimitive.ScrollToBottom asChild>
                  <Button type="button" size="icon" variant="outline" className="absolute -top-12 self-center rounded-full bg-background shadow-sm disabled:invisible" aria-label="滚动到底部"><ArrowDown /></Button>
                </ThreadPrimitive.ScrollToBottom>

                {selectedMessageIds.length ? <div className="animate-in fade-in slide-in-from-bottom-2 flex items-center justify-between gap-3 rounded-2xl border bg-background px-3 py-2 shadow-lg duration-200 motion-reduce:animate-none" aria-label="已选择的消息">
                  <p className="min-w-0 truncate text-sm">已选择 {selectedMessageIds.length} 条消息和 {selectedAttachments.length} 个附件</p>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button type="button" size="icon-sm" variant="ghost" className="rounded-full" aria-label="取消选择" onClick={() => setSelectedMessageIds([])}><X /></Button>
                    <Dialog open={handoffOpen} onOpenChange={setHandoffOpen}>
                      <DialogTrigger render={<Button type="button" size="sm" className="rounded-full px-3" />}>转交事件调查</DialogTrigger>
                      <DialogContent className="max-w-xl">
                        <DialogHeader>
                          <DialogTitle>转交事件调查</DialogTitle>
                          <DialogDescription>仅复制选中的已完成消息及其已就绪附件作为 Human Input。对话内容不会成为 Evidence、Approval 或执行授权。</DialogDescription>
                        </DialogHeader>
                        <div className="grid gap-4">
                          <div className="grid gap-2">
                            <label className="text-sm font-medium">转交方式</label>
                            <Select value={handoffTargetType} onValueChange={(value) => setHandoffTargetType(value as typeof handoffTargetType)}>
                              <SelectTrigger aria-label="转交目标类型" className="w-full"><SelectValue>{handoffTargetType === "existing_incident" ? "已有 Incident" : "用户创建事件"}</SelectValue></SelectTrigger>
                              <SelectContent><SelectGroup><SelectItem value="existing_incident">已有 Incident</SelectItem><SelectItem value="user_created_incident">用户创建事件</SelectItem></SelectGroup></SelectContent>
                            </Select>
                          </div>
                          {handoffTargetType === "existing_incident" ? <div className="grid gap-2">
                            <label className="text-sm font-medium">目标 Incident</label>
                            <Select value={targetIncidentId} onValueChange={(value) => setIncidentId(value ?? "")}>
                              <SelectTrigger aria-label="目标事件" className="w-full"><SelectValue>{targetIncident?.title ?? "选择 Incident"}</SelectValue></SelectTrigger>
                              <SelectContent><SelectGroup>{incidents.map((incident) => <SelectItem key={incident.id} value={incident.id}>{incident.title}</SelectItem>)}</SelectGroup></SelectContent>
                            </Select>
                          </div> : <>
                            <div className="grid gap-2">
                              <label className="text-sm font-medium">真实资源</label>
                              <Select value={handoffResourceId} onValueChange={(value) => setHandoffResourceId(value ?? "")}>
                                <SelectTrigger aria-label="用户创建事件资源" className="w-full"><SelectValue>{handoffResource ? `${handoffResource.cluster_id} / ${handoffResource.namespace} / ${handoffResource.name}` : "选择真实资源"}</SelectValue></SelectTrigger>
                                <SelectContent><SelectGroup>{resources.map((resource) => <SelectItem key={resource.id} value={resource.id}>{resource.cluster_id} / {resource.namespace} / {resource.name}{resource.binding_state === "unbound" ? "（未绑定）" : ""}</SelectItem>)}</SelectGroup></SelectContent>
                              </Select>
                            </div>
                            <div className="grid gap-2">
                              <label htmlFor="handoff-summary" className="text-sm font-medium">问题摘要</label>
                              <Textarea id="handoff-summary" value={problemSummary} onChange={(event) => setProblemSummary(event.target.value)} maxLength={2000} placeholder="描述需要正式调查的问题" />
                              {handoffResource?.binding_state === "unbound" ? <p className="text-sm text-destructive" role="alert">所选资源未绑定到有效 Service，不能创建 Incident。</p> : null}
                            </div>
                          </>}
                          <div className="rounded-lg bg-muted/60 p-3 text-sm">
                            <p className="font-medium">核对转交内容</p>
                            <p className="mt-1 text-muted-foreground">{selectedMessageIds.length} 条消息和 {selectedAttachments.length} 个已就绪附件将作为 Human Input，未选消息不会转移。</p>
                            <p className="mt-1 break-words">目标：{handoffTargetType === "existing_incident" ? targetIncident?.title ?? "未选择 Incident" : problemSummary.trim() || "未填写问题摘要"}</p>
                          </div>
                          {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
                        </div>
                        <DialogFooter>
                          <DialogClose render={<Button type="button" variant="outline" />}>取消</DialogClose>
                          <Button type="button" onClick={submitHandoff} disabled={busy || !canHandoff}>确认转交</Button>
                        </DialogFooter>
                      </DialogContent>
                    </Dialog>
                  </div>
                </div> : null}

                <ComposerPrimitive.Root className="relative flex w-full flex-col">
                  <ComposerPrimitive.AttachmentDropzone disabled={locked || attachmentBusy || composerAttachments.length >= 5} className="flex w-full flex-col gap-2 rounded-[1.5rem] border border-border/60 bg-muted/40 p-2 shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] outline-none transition-[border-color,box-shadow] focus-within:border-border focus-within:shadow-[0_6px_24px_-8px_rgba(0,0,0,0.12),0_1px_2px_rgba(0,0,0,0.05)] data-[dragging=true]:border-dashed data-[dragging=true]:border-ring data-[dragging=true]:bg-accent/60">
                    <ChatComposerAttachments attachments={attachments} busy={attachmentBusy} onRemove={onRemoveAttachment} onRetry={onRetryAttachment} />
                    <ComposerPrimitive.Input aria-label="输入消息" placeholder="询问 AIOps 或 Kubernetes 知识" maxLength={8000} className="max-h-32 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base outline-none placeholder:text-muted-foreground/80" />
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-1">
                        <Tooltip><TooltipTrigger render={composerAttachments.length >= 5
                          ? <Button type="button" size="icon-sm" variant="ghost" className="rounded-full" aria-label="选择附件" disabled />
                          : <ComposerPrimitive.AddAttachment multiple render={<Button type="button" size="icon-sm" variant="ghost" className="rounded-full" aria-label="选择附件" />} />
                        }><Paperclip /></TooltipTrigger><TooltipContent side="bottom">选择附件，最多 5 个</TooltipContent></Tooltip>
                        <Select value={selectedTargetId} onValueChange={(value) => onScopeChange(value ?? "knowledge")}>
                          <SelectTrigger size="sm" aria-label="AI 对话环境范围" className="max-w-[min(20rem,55vw)] rounded-full border-transparent px-2 shadow-none hover:bg-muted"><SelectValue>
                            {selectedResource ? `${selectedResource.cluster_id} / ${selectedResource.namespace} / ${selectedResource.name}` : "仅知识问答"}
                          </SelectValue></SelectTrigger>
                          <SelectContent><SelectGroup><SelectItem value="knowledge">仅知识问答</SelectItem>{resources.map((resource) => <SelectItem key={resource.id} value={resource.id}>{resource.cluster_id} / {resource.namespace} / {resource.kind} / {resource.name}</SelectItem>)}</SelectGroup></SelectContent>
                        </Select>
                      </div>
                      {responseRunning
                        ? <Tooltip><TooltipTrigger render={<ComposerPrimitive.Cancel render={<Button type="button" size="icon-sm" className="rounded-full" aria-label="停止生成" />} />}><Square className="size-3 fill-current" /></TooltipTrigger><TooltipContent side="bottom">停止生成</TooltipContent></Tooltip>
                        : <Tooltip><TooltipTrigger render={<ComposerPrimitive.Send render={<Button type="submit" size="icon-sm" className="rounded-full" aria-label="发送" />} />}><ArrowUp /></TooltipTrigger><TooltipContent side="bottom">发送</TooltipContent></Tooltip>}
                    </div>
                  </ComposerPrimitive.AttachmentDropzone>
                  <p className="px-4 pt-2 text-center text-xs text-muted-foreground">{selectedResource ? "仅查询当前冻结范围内的只读数据" : "知识问答不会查询实时环境"}</p>
                </ComposerPrimitive.Root>
              </ThreadPrimitive.ViewportFooter>
            </ThreadPrimitive.Viewport>
            </ThreadPrimitive.Root>
          </>
        ) : loading ? (
          <div className="grid flex-1 place-items-center p-6 text-center"><p className="animate-pulse text-sm text-muted-foreground motion-reduce:animate-none" role="status">正在加载 AI 对话...</p></div>
        ) : (
          <div className="grid flex-1 place-items-center p-6 text-center">
            <div><Sheet><SheetTrigger render={<Button type="button" size="sm" variant="outline" className="mb-4 rounded-full md:hidden" />}><Menu />打开会话栏</SheetTrigger><SheetContent side="left" className="w-[min(22rem,90vw)] gap-0 p-0"><SheetHeader className="sr-only"><SheetTitle>AI 对话会话</SheetTitle><SheetDescription>搜索、筛选和切换 AI 对话会话。</SheetDescription></SheetHeader>{threadList()}</SheetContent></Sheet><h2 className="text-2xl font-semibold">今天需要排查什么？</h2><p className="mt-2 text-sm text-muted-foreground">选择已有会话，或新建 AI 对话。</p><Button type="button" className="mt-5 rounded-full px-4" onClick={createOrSelect} disabled={busy}>新建对话</Button></div>
          </div>
        )}
        {error && !handoffOpen ? <p className="border-t p-3 text-sm text-destructive" role="alert">{error}</p> : null}
      </section>
    </main>
    <Dialog open={Boolean(handoff)} onOpenChange={(open) => { if (!open) onDismissHandoff() }}>
      {handoff ? <DialogContent>
        <HandoffSuccessContent handoff={handoff} onOpenInvestigation={onOpenInvestigation} />
      </DialogContent> : null}
    </Dialog>
    </AssistantRuntimeProvider>
  )
}

export function ChatPage() {
  const {sessionId} = useParams()
  const navigate = useNavigate()
  const [selectedTargetId, setSelectedTargetId] = useState("knowledge")
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<"all" | "normal" | "pinned" | "archived">("all")
  const [attachmentFailure, setAttachmentFailure] = useState<unknown | null>(null)
  const [attachmentActionBusy, setAttachmentActionBusy] = useState(false)
  const chat = useChatSessionController({
    sessionId,
    query,
    filter,
    onCreated: (id) => navigate(`/chat/${id}`),
    onDeleted: (id) => { if (sessionId === id) navigate("/chat") },
  })
  const resources = useQuery({queryKey: ["resource-workspace"], queryFn: listResourceWorkspace})
  const incidents = useQuery({queryKey: ["incidents"], queryFn: listIncidents})
  const attachmentAdapter = sessionId ? chatAttachmentAdapter({
    reserve: (file) => reserveChatAttachment(sessionId, file),
    upload: (attachmentId, file) => uploadChatAttachment(sessionId, attachmentId, file),
    retry: (attachmentId) => retryChatAttachment(sessionId, attachmentId),
    remove: chat.actions.deleteAttachment,
    onChange: chat.actions.updateAttachment,
    onError: setAttachmentFailure,
  }) : undefined

  useEffect(() => {
    setSelectedTargetId(chat.state.session?.selected_scope?.selection.deployment_target_id ?? "knowledge")
    setAttachmentFailure(null)
  }, [sessionId, chat.state.session?.selected_scope?.revision])

  const failure = chat.errors.action ?? attachmentFailure ?? chat.errors.attachment ?? chat.errors.query
  const error = chatErrorMessage(failure)
  return (
    <ChatView
      sessions={chat.state.sessions}
      session={chat.state.session}
      pendingContent={chat.state.pendingContent}
      connection={chat.state.connection}
      resources={resources.data?.resources ?? []}
      incidents={incidents.data ?? []}
      selectedTargetId={selectedTargetId}
      generating={chat.state.generating}
      loading={chat.state.loading}
      actionBusy={chat.state.actionBusy}
      query={query}
      filter={filter}
      busy={chat.state.busy || attachmentActionBusy}
      attachments={chat.state.attachments}
      attachmentBusy={chat.state.attachmentBusy || attachmentActionBusy}
      attachmentAdapter={attachmentAdapter}
      error={error}
      handoff={chat.state.handoff}
      onCreate={chat.actions.create}
      onSelect={(id) => navigate(`/chat/${id}`)}
      onQueryChange={setQuery}
      onFilterChange={setFilter}
      onRename={(id, title) => chat.actions.update(id, {title})}
      onPin={(id, pinned) => chat.actions.update(id, {pinned})}
      onArchive={(id, archived) => chat.actions.update(id, {archived})}
      onDelete={chat.actions.remove}
      onSend={(content, attachmentIds) => {
        const resource = resources.data?.resources.find((item) => item.id === selectedTargetId)
        chat.actions.send(content, resource ? {cluster_id: resource.cluster_id, deployment_target_id: resource.id} : undefined, attachmentIds)
      }}
      onCancel={chat.actions.cancel}
      onRemoveAttachment={chat.actions.removeAttachment}
      onRetryAttachment={(attachmentId) => {
        if (!attachmentAdapter) return
        setAttachmentActionBusy(true)
        void attachmentAdapter.retry(attachmentId).catch(() => undefined).finally(() => {
          setAttachmentActionBusy(false)
          void chat.actions.refreshAttachments()
        })
      }}
      onScopeChange={setSelectedTargetId}
      onRetry={chat.actions.retry}
      onEdit={chat.actions.edit}
      onReload={chat.actions.reload}
      onSwitchBranch={chat.actions.switchBranch}
      onHandoff={chat.actions.handoff}
      onDismissHandoff={chat.actions.dismissHandoff}
      onOpenInvestigation={(incidentId) => navigate(`/incidents/${incidentId}`)}
    />
  )
}
