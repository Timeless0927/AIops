import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useParams } from "react-router"
import { Archive, ArchiveRestore, ChevronLeft, ChevronRight, Download, MoreHorizontal, Paperclip, Pencil, Pin, PinOff, RefreshCw, Search, Trash2 } from "lucide-react"
import {
  AssistantRuntimeProvider,
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  ThreadPrimitive,
  type ThreadMessage,
  useExternalStoreRuntime,
} from "@assistant-ui/react"

import {
  ApiError,
  createChatHandoff,
  createChatSession,
  chatAttachmentDownloadUrl,
  deleteChatAttachment,
  editChatMessage,
  getChatSession,
  listIncidents,
  listChatSessions,
  listChatAttachments,
  listResourceWorkspace,
  newClientId,
  retryChatMessage,
  reloadChatMessage,
  reserveChatAttachment,
  sendChatMessage,
  switchChatBranch,
  retryChatAttachment,
  uploadChatAttachment,
  updateChatSession,
  deleteChatSession,
  type ChatScopeSelection,
  type ChatHandoff,
  type ChatHandoffTarget,
  type ChatAttachment,
  type ChatSession,
  type ChatSessionSummary,
  type Incident,
  type ResourceWorkspace,
} from "@/api/client"
import { chatMessageRepository, chatThreadListAdapter, textFromAssistantMessage, type GatewayMessageMetadata } from "@/chat/chat-runtime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

type ChatViewProps = {
  sessions: ChatSessionSummary[]
  session: ChatSession | null
  pendingContent: string | null
  connection: "connecting" | "connected" | "reconnecting"
  resources: ResourceWorkspace["resources"]
  incidents: Incident[]
  selectedTargetId: string
  busy: boolean
  error: string | null
  handoff: ChatHandoff | null
  actionBusy: boolean
  query: string
  filter: "all" | "normal" | "pinned" | "archived"
  attachments?: ChatAttachment[]
  attachmentBusy?: boolean
  onCreate: () => void
  onSelect: (sessionId: string) => void
  onQueryChange: (query: string) => void
  onFilterChange: (filter: "all" | "normal" | "pinned" | "archived") => void
  onRename: (sessionId: string, title: string) => void
  onPin: (sessionId: string, pinned: boolean) => void
  onArchive: (sessionId: string, archived: boolean) => void
  onDelete: (sessionId: string) => void
  onSend: (content: string, attachmentIds: string[]) => void
  onFiles?: (files: FileList) => void
  onRemoveAttachment?: (attachmentId: string) => void
  onRetryAttachment?: (attachmentId: string) => void
  onScopeChange: (targetId: string) => void
  onRetry: (messageId: string) => void
  onEdit: (messageId: string, content: string) => void
  onReload: (messageId: string) => void
  onSwitchBranch: (messageId: string) => void
  onHandoff: (messageIds: string[], target: ChatHandoffTarget) => void
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

export function ChatView({
  sessions,
  session,
  pendingContent,
  connection,
  resources,
  incidents,
  selectedTargetId,
  busy,
  error,
  handoff,
  actionBusy,
  query,
  filter,
  attachments = [],
  attachmentBusy = false,
  onCreate,
  onSelect,
  onQueryChange,
  onFilterChange,
  onRename,
  onPin,
  onArchive,
  onDelete,
  onSend,
  onFiles = () => undefined,
  onRemoveAttachment = () => undefined,
  onRetryAttachment = () => undefined,
  onScopeChange,
  onRetry,
  onEdit,
  onReload,
  onSwitchBranch,
  onHandoff,
}: ChatViewProps) {
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameTitle, setRenameTitle] = useState("")
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [selectedMessageIds, setSelectedMessageIds] = useState<string[]>([])
  const [handoffTargetType, setHandoffTargetType] = useState<"existing_incident" | "user_created_incident">("existing_incident")
  const [incidentId, setIncidentId] = useState("")
  const [problemSummary, setProblemSummary] = useState("")
  const [handoffResourceId, setHandoffResourceId] = useState("")
  const selectedResource = resources.find((resource) => resource.id === selectedTargetId)
  const targetIncidentId = incidentId || incidents[0]?.id || ""
  const targetIncident = incidents.find((incident) => incident.id === targetIncidentId)
  const handoffResource = resources.find((resource) => resource.id === handoffResourceId)
  const canHandoff = selectedMessageIds.length > 0 && (handoffTargetType === "existing_incident"
    ? Boolean(targetIncidentId)
    : Boolean(problemSummary.trim() && handoffResource?.binding_state === "bound"))
  const locked = busy || Boolean(session?.messages.some((message) => message.status === "sending"))
  const composerAttachments = attachments.filter((attachment) => !attachment.message_id)
  const readyAttachmentIds = composerAttachments.filter((attachment) => attachment.status === "ready").map((attachment) => attachment.id)
  const attachmentPending = composerAttachments.some((attachment) => attachment.status !== "ready")
  const repository = useMemo(() => chatMessageRepository(session), [session])
  const threadListAdapter = useMemo(() => chatThreadListAdapter({
    threadId: session?.id,
    sessions,
    archived: filter === "archived",
    onCreate,
    onSelect,
    onRename,
    onPin,
    onArchive,
    onDelete,
  }), [filter, onArchive, onCreate, onDelete, onPin, onRename, onSelect, session?.id, sessions])
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning: locked,
    isDisabled: locked,
    isSendDisabled: attachmentPending,
    onNew: async (message) => {
      const content = textFromAssistantMessage(message.content)
      if (content.trim() && !attachmentPending) onSend(content, readyAttachmentIds)
    },
    onEdit: async (message) => {
      const content = textFromAssistantMessage(message.content)
      if (message.sourceId && content.trim()) onEdit(message.sourceId, content)
    },
    onReload: async (_parentId, config) => {
      if (config.sourceId) onReload(config.sourceId)
    },
    onRefetchThread: async () => undefined,
    adapters: {threadList: threadListAdapter},
    setMessages: () => undefined,
    unstable_onBranchChange: ({headId}) => { if (headId) onSwitchBranch(headId) },
  })

  useEffect(() => {
    setSelectedMessageIds([])
    setProblemSummary("")
    setIncidentId("")
    setHandoffResourceId("")
  }, [session?.id])

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
  function startRename(item: ChatSessionSummary) {
    setRenameId(item.id)
    setRenameTitle(item.title)
  }

  function commitRename() {
    const title = renameTitle.trim()
    if (!renameId || !title) return
    onRename(renameId, title)
    setRenameId(null)
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
    <main className="mx-auto grid min-h-[calc(100vh-3.25rem)] max-w-[1600px] min-w-0 md:grid-cols-[18rem_1fr]">
      <aside className="min-w-0 border-b p-4 md:border-r md:border-b-0">
        <div className="flex items-start justify-between gap-3">
          <div><h1 className="text-xl font-semibold">AI 对话</h1><p className="mt-1 text-xs text-muted-foreground">对话长期保留，直到主动删除</p></div>
          <Button size="sm" onClick={onCreate} disabled={busy || actionBusy}>新建对话</Button>
        </div>
        <div className="mt-4 space-y-2">
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input aria-label="搜索 AI 对话" value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="搜索标题或消息" className="pl-8" />
          </div>
          <Select value={filter} onValueChange={(value) => onFilterChange(value as typeof filter)}>
            <SelectTrigger aria-label="筛选 AI 对话" className="w-full"><SelectValue>{filter === "all" ? "正常会话" : filter === "normal" ? "未置顶" : filter === "pinned" ? "已置顶" : "已归档"}</SelectValue></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">正常会话</SelectItem>
              <SelectItem value="normal">未置顶</SelectItem>
              <SelectItem value="pinned">已置顶</SelectItem>
              <SelectItem value="archived">已归档</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <nav aria-label="AI 对话会话" className="mt-4 space-y-1">
          <ThreadListPrimitive.Root className="space-y-1">
            <ThreadListPrimitive.Items archived={filter === "archived"}>
              {({threadListItem}) => {
                const item = sessions.find((candidate) => candidate.id === threadListItem.id)
                if (!item) return null
                return (
                  <ThreadListItemPrimitive.Root key={item.id} className={`group flex min-w-0 items-center gap-1 rounded-lg ${item.id === session?.id ? "bg-secondary" : "hover:bg-muted"}`}>
                    {renameId === item.id ? (
                      <Input
                        aria-label="重命名 AI 对话"
                        autoFocus
                        value={renameTitle}
                        onChange={(event) => setRenameTitle(event.target.value)}
                        onKeyDown={(event) => { if (event.key === "Enter") commitRename(); if (event.key === "Escape") setRenameId(null) }}
                        className="h-8 min-w-0 flex-1"
                      />
                    ) : (
                      <ThreadListItemPrimitive.Trigger
                        render={<Button variant="ghost" className="h-auto min-w-0 flex-1 justify-start px-3 py-2 text-left hover:bg-transparent" />}
                      >
                        <span className="min-w-0">
                          <span className="flex min-w-0 items-center gap-1 truncate">
                            {item.pinned ? <Pin className="size-3 shrink-0 text-primary" aria-label="已置顶" /> : null}
                            <span className="truncate">{item.title}</span>
                          </span>
                          <span className="block text-xs text-muted-foreground">{item.message_count} 条消息</span>
                        </span>
                      </ThreadListItemPrimitive.Trigger>
                    )}
                    {renameId === item.id ? (
                      <Button size="icon-sm" variant="ghost" aria-label="保存标题" onClick={commitRename} disabled={!renameTitle.trim()}><Pencil /></Button>
                    ) : (
                      <DropdownMenu>
                        <DropdownMenuTrigger render={<Button size="icon-sm" variant="ghost" aria-label={`操作 ${item.title}`} />}>
                          <MoreHorizontal />
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-44">
                          <DropdownMenuItem onClick={() => startRename(item)}><Pencil />重命名</DropdownMenuItem>
                          <DropdownMenuItem onClick={() => onPin(item.id, !item.pinned)}>{item.pinned ? <PinOff /> : <Pin />} {item.pinned ? "取消置顶" : "置顶"}</DropdownMenuItem>
                          <DropdownMenuItem onClick={() => onArchive(item.id, !item.archived)}>{item.archived ? <ArchiveRestore /> : <Archive />} {item.archived ? "恢复会话" : "归档会话"}</DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem variant="destructive" onClick={() => setDeleteId(item.id)}><Trash2 />永久删除</DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </ThreadListItemPrimitive.Root>
                )
              }}
            </ThreadListPrimitive.Items>
          </ThreadListPrimitive.Root>
          {!sessions.length ? <p className="py-8 text-center text-sm text-muted-foreground">{query.trim() ? "没有匹配的 AI 对话" : filter === "archived" ? "没有已归档会话" : "尚无 AI 对话会话"}</p> : null}
        </nav>
        <AlertDialog open={Boolean(deleteId)} onOpenChange={(open) => { if (!open) setDeleteId(null) }}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>永久删除 AI 对话？</AlertDialogTitle>
              <AlertDialogDescription>此操作会永久删除会话和未被事件调查引用的聊天数据，无法恢复。</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>取消</AlertDialogCancel>
              <AlertDialogAction onClick={() => { if (deleteId) onDelete(deleteId); setDeleteId(null) }}>永久删除</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-col" aria-label="AI 对话消息">
        {session ? (
          <>
            <header className="flex items-center justify-between gap-3 border-b px-4 py-3">
              <h2 className="truncate font-medium">{session.title}</h2>
              <p className="shrink-0 text-xs text-muted-foreground" role="status" aria-live="polite">
                {connection === "connected" ? "实时更新已连接" : connection === "reconnecting" ? "连接已断开，正在恢复实时更新" : "正在连接实时更新"}
              </p>
            </header>
            <div className="flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
              <ThreadPrimitive.Messages>
              {({message: runtimeMessage}) => {
                const message = session.messages.find((candidate) => candidate.id === runtimeMessage.id)
                if (!message) return null
                const gateway = runtimeMessage.metadata.custom.gateway as GatewayMessageMetadata | undefined
                return (
                <MessagePrimitive.Root asChild>
                <article
                  className={message.role === "user" ? "ml-auto max-w-2xl rounded-lg bg-primary px-4 py-3 text-primary-foreground" : "max-w-2xl rounded-lg border bg-card px-4 py-3"}
                >
                  <div className="mb-1 flex items-center gap-2 text-xs opacity-70">
                    <span>{message.role === "user" ? "你" : "AIOps"}</span>
                    {message.status === "sending" ? <Badge variant="outline">正在回答</Badge> : null}
                    {message.status === "failed" ? <Badge variant="destructive">回答失败</Badge> : null}
                  </div>
                  <div className="break-words text-sm"><MessagePrimitive.Parts /></div>
                  {(gateway?.scope ?? message.scope)?.resources.length ? <div className="mt-3 border-t pt-2 text-xs">
                    <p className="font-medium">环境范围</p>
                    {(gateway?.scope ?? message.scope)!.resources.map((resource) => <p key={resource.deployment_target_id} className="mt-1 break-words text-muted-foreground">
                      {resource.cluster_id} / {resource.namespace} / {resource.workload_kind} / {resource.workload_name}
                    </p>)}
                  </div> : null}
                  {(gateway?.toolActivity ?? message.tool_activity).length ? <section className="mt-3 border-t pt-2 text-xs" aria-label="工具活动">
                    <p className="font-medium">工具活动</p>
                    <ul className="mt-1 space-y-2">{(gateway?.toolActivity ?? message.tool_activity).map((activity, index) => <li key={`${activity.tool}-${index}`}>
                      <div className="flex flex-wrap items-center gap-2"><span>{activity.tool}</span><Badge variant="outline">{activity.status}</Badge></div>
                      <p className="mt-1 break-words text-muted-foreground">{activity.summary}</p>
                      {activity.missing_reason ? <p className="mt-1 break-words text-muted-foreground">{activity.missing_reason}</p> : null}
                    </li>)}</ul>
                  </section> : null}
                  {(gateway?.evidenceReferences ?? message.evidence_references).length ? <div className="mt-3 border-t pt-2 text-xs"><p className="font-medium">引用</p><ul className="mt-1 space-y-1 text-muted-foreground">{(gateway?.evidenceReferences ?? message.evidence_references).map((reference) => <li key={reference} className="break-all">{reference}</li>)}</ul></div> : null}
                  {(gateway?.uncertainty ?? message.uncertainty) ? <p className="mt-2 text-xs text-muted-foreground">不确定性：{(gateway?.uncertainty ?? message.uncertainty)!.status}{(gateway?.uncertainty ?? message.uncertainty)!.reasons.length ? ` · ${(gateway?.uncertainty ?? message.uncertainty)!.reasons.join("；")}` : ""}</p> : null}
                  {(gateway?.nextStep ?? message.next_step) ? <p className="mt-2 text-xs"><span className="font-medium">下一步：</span>{gateway?.nextStep ?? message.next_step}</p> : null}
                  {(gateway?.completion ?? message.completion) ? <p className="mt-2 text-xs text-muted-foreground">完成：{(gateway?.completion ?? message.completion)!.status} · {(gateway?.completion ?? message.completion)!.stopping_reason}</p> : null}
                  {(gateway?.skillVersions ?? message.skill_versions).length ? <p className="mt-2 text-xs text-muted-foreground">技能版本：{(gateway?.skillVersions ?? message.skill_versions).map((skill) => `${skill.name} v${skill.version}`).join("、")}</p> : null}
                  {message.status === "failed" ? <Button className="mt-2" size="sm" variant="outline" onClick={() => onRetry(message.id)} disabled={locked}>重试</Button> : null}
                  {runtimeMessage.composer.isEditing ? <MessageEditComposer /> : <ActionBarPrimitive.Root className="mt-2 flex items-center gap-2 border-t pt-2">
                    {message.role === "user" && message.status === "completed" ? <ActionBarPrimitive.Edit render={<Button size="sm" variant="ghost" />}><Pencil />编辑</ActionBarPrimitive.Edit> : null}
                    {message.role === "assistant" && message.status === "completed" ? locked
                      ? <Button size="sm" variant="ghost" disabled><RefreshCw />重新生成</Button>
                      : <ActionBarPrimitive.Reload render={<Button size="sm" variant="ghost" />}><RefreshCw />重新生成</ActionBarPrimitive.Reload>
                    : null}
                  </ActionBarPrimitive.Root>}
                  {message.branch_count > 1 ? <BranchPickerPrimitive.Root className="mt-2 flex items-center gap-1 border-t pt-2 text-xs">
                      <BranchPickerPrimitive.Previous aria-label="上一分支"><ChevronLeft />上一分支</BranchPickerPrimitive.Previous>
                      <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
                      <BranchPickerPrimitive.Next aria-label="下一分支"><ChevronRight />下一分支</BranchPickerPrimitive.Next>
                    </BranchPickerPrimitive.Root> : null}
                  {message.status === "completed" && message.content ? <label className="mt-3 flex cursor-pointer items-center gap-2 border-t pt-2 text-xs">
                    <Checkbox
                      aria-label={`选择消息 ${message.content}`}
                      checked={selectedMessageIds.includes(message.id)}
                      onCheckedChange={(checked) => setSelectedMessageIds((current) => checked ? [...current, message.id] : current.filter((id) => id !== message.id))}
                    />
                    <span>选择此消息用于 Handoff</span>
                  </label> : null}
                </article>
                </MessagePrimitive.Root>
                )
              }}
              </ThreadPrimitive.Messages>
              {pendingContent ? <article className="ml-auto max-w-2xl rounded-lg bg-primary px-4 py-3 text-primary-foreground"><p className="whitespace-pre-wrap break-words text-sm">{pendingContent}</p><span className="mt-1 block text-xs opacity-70">正在发送</span></article> : null}
            </div>
            <section className="border-t p-4" aria-label="转交事件调查">
              <h3 className="font-medium">转交事件调查</h3>
              <p className="mt-1 text-xs text-muted-foreground">仅复制选中的已完成消息作为 Human Input；AI 对话内容不会成为 Evidence、Approval 或执行授权。</p>
              <p className="mt-2 text-sm">已选择 {selectedMessageIds.length} 条消息。可关联已有 Incident，或创建用户创建事件（User-created Incident）。</p>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <Select value={handoffTargetType} onValueChange={(value) => setHandoffTargetType(value as typeof handoffTargetType)}>
                  <SelectTrigger aria-label="转交目标类型" className="w-full"><SelectValue>{handoffTargetType === "existing_incident" ? "已有 Incident" : "用户创建事件"}</SelectValue></SelectTrigger>
                  <SelectContent><SelectGroup><SelectItem value="existing_incident">已有 Incident</SelectItem><SelectItem value="user_created_incident">用户创建事件</SelectItem></SelectGroup></SelectContent>
                </Select>
                {handoffTargetType === "existing_incident" ? (
                  <Select value={targetIncidentId} onValueChange={(value) => setIncidentId(value ?? "")}>
                    <SelectTrigger aria-label="目标事件" className="w-full"><SelectValue>{targetIncident?.title ?? "选择 Incident"}</SelectValue></SelectTrigger>
                    <SelectContent><SelectGroup>{incidents.map((incident) => <SelectItem key={incident.id} value={incident.id}>{incident.title}</SelectItem>)}</SelectGroup></SelectContent>
                  </Select>
                ) : (
                  <Select value={handoffResourceId} onValueChange={(value) => setHandoffResourceId(value ?? "")}>
                    <SelectTrigger aria-label="用户创建事件资源" className="w-full"><SelectValue>{handoffResource ? `${handoffResource.cluster_id} / ${handoffResource.namespace} / ${handoffResource.name}` : "选择真实资源"}</SelectValue></SelectTrigger>
                    <SelectContent><SelectGroup>{resources.map((resource) => <SelectItem key={resource.id} value={resource.id}>{resource.cluster_id} / {resource.namespace} / {resource.name}{resource.binding_state === "unbound" ? "（未绑定）" : ""}</SelectItem>)}</SelectGroup></SelectContent>
                  </Select>
                )}
              </div>
              {handoffTargetType === "user_created_incident" ? <div className="mt-3">
                <label htmlFor="handoff-summary" className="text-sm font-medium">问题摘要</label>
                <Textarea id="handoff-summary" value={problemSummary} onChange={(event) => setProblemSummary(event.target.value)} maxLength={2000} placeholder="描述需要正式调查的问题" />
                {handoffResource?.binding_state === "unbound" ? <p className="mt-1 text-sm text-destructive" role="alert">所选资源未绑定到有效 Service，不能创建 Incident。</p> : null}
              </div> : null}
              <details className="mt-3 rounded-lg border p-3">
                <summary className="cursor-pointer text-sm font-medium">核对转交内容</summary>
                <div className="mt-2 text-sm">
                  <p>{selectedMessageIds.length} 条消息将作为 Human Input，未选消息不会转移。</p>
                  <p className="mt-1">目标：{handoffTargetType === "existing_incident" ? targetIncident?.title ?? "未选择 Incident" : problemSummary.trim() || "未填写问题摘要"}</p>
                  <p className="mt-1 text-muted-foreground">确认后仍适用原有权限、Evidence Gate 和生命周期规则。</p>
                  <Button className="mt-3" type="button" onClick={submitHandoff} disabled={busy || !canHandoff}>确认转交</Button>
                </div>
              </details>
              {handoff ? <p className="mt-3 text-sm" role="status">{handoff.idempotent ? "重复请求已安全返回" : "Handoff 已完成"}：Incident {handoff.incident_id}</p> : null}
            </section>
            <ComposerPrimitive.Root className="border-t p-4">
              <Select value={selectedTargetId} onValueChange={(value) => onScopeChange(value ?? "knowledge")}>
                <SelectTrigger aria-label="AI 对话环境范围" className="mb-2 w-full"><SelectValue>
                  {selectedResource ? `${selectedResource.cluster_id} / ${selectedResource.namespace} / ${selectedResource.kind} / ${selectedResource.name}` : "仅知识问答"}
                </SelectValue></SelectTrigger>
                <SelectContent><SelectGroup><SelectItem value="knowledge">仅知识问答</SelectItem>{resources.map((resource) => <SelectItem key={resource.id} value={resource.id}>{resource.cluster_id} / {resource.namespace} / {resource.kind} / {resource.name}</SelectItem>)}</SelectGroup></SelectContent>
              </Select>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-md border px-3 text-sm hover:bg-muted focus-within:ring-2 focus-within:ring-ring">
                  <Paperclip className="size-4" aria-hidden="true" />选择附件
                  <input
                    className="sr-only"
                    type="file"
                    multiple
                    accept=".png,.jpg,.jpeg,.webp,.pdf,.txt,.log,.md,.markdown,.json,.yaml,.yml,.csv"
                    aria-label="选择附件"
                    disabled={locked || attachmentBusy || composerAttachments.length >= 5}
                    onChange={(event) => { if (event.target.files?.length) onFiles(event.target.files); event.target.value = "" }}
                  />
                </label>
                <span className="text-xs text-muted-foreground">最多 5 个，单个 20MB，总计 50MB</span>
              </div>
              {composerAttachments.length ? <ul className="mb-2 divide-y border-y" aria-label="待发送附件">
                {composerAttachments.map((attachment) => <li key={attachment.id} className="flex min-w-0 items-center gap-2 py-2 text-sm">
                  <span className="min-w-0 flex-1 truncate">{attachment.filename}</span>
                  <Badge variant={attachment.status === "rejected" || attachment.status === "failed" ? "destructive" : "outline"}>
                    {{pending: "等待上传", uploading: "上传中", scanning: "安全扫描中", ready: "已就绪", rejected: "已拒绝", failed: "处理失败"}[attachment.status]}
                  </Badge>
                  {attachment.rejection_code ? <span className="max-w-48 truncate text-xs text-destructive">{{sensitive_content: "疑似凭据或 Secure Input", sensitive_filename: "疑似凭据或证书文件", malware_detected: "恶意文件扫描未通过", scanner_unavailable: "安全扫描服务不可用", scanner_error: "安全扫描失败", storage_unavailable: "附件存储暂时不可用", parse_failed: "文件解析失败", parse_limit: "文件内容超过解析限制", mime_mismatch: "文件格式与声明不一致", attachment_too_large: "文件超过 20MB"}[attachment.rejection_code] ?? "无法处理附件"}</span> : null}
                  {attachment.status === "ready" ? <Button type="button" size="sm" variant="ghost" render={<a href={chatAttachmentDownloadUrl(attachment.session_id, attachment.id)} />}><Download />下载</Button> : null}
                  {attachment.status === "failed" ? <Button type="button" size="sm" variant="ghost" onClick={() => onRetryAttachment(attachment.id)} disabled={attachmentBusy}><RefreshCw />重试</Button> : null}
                  <Button type="button" size="sm" variant="ghost" onClick={() => onRemoveAttachment(attachment.id)} disabled={attachmentBusy}><Trash2 />移除</Button>
                </li>)}
              </ul> : null}
              <ComposerPrimitive.Input aria-label="输入消息" placeholder="询问 AIOps 或 Kubernetes 知识" maxLength={8000} className="min-h-20 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm" />
              <div className="mt-2 flex items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">{selectedResource ? "环境问题只查询本次冻结范围内的只读数据。" : "知识问答不会查询实时环境。"}</p>
                <ComposerPrimitive.Send render={<Button type="submit" />}>发送</ComposerPrimitive.Send>
              </div>
            </ComposerPrimitive.Root>
          </>
        ) : (
          <div className="grid flex-1 place-items-center p-6 text-center">
            <div><h2 className="font-medium">选择或新建 AI 对话会话</h2><p className="mt-2 text-sm text-muted-foreground">知识问答不会创建 Evidence、Approval 或 Connector Command。</p></div>
          </div>
        )}
        {error ? <p className="border-t p-3 text-sm text-destructive" role="alert">{error}</p> : null}
      </section>
    </main>
    </AssistantRuntimeProvider>
  )
}

export function ChatPage() {
  const {sessionId} = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [pendingContent, setPendingContent] = useState<string | null>(null)
  const [connection, setConnection] = useState<"connecting" | "connected" | "reconnecting">("connecting")
  const [selectedTargetId, setSelectedTargetId] = useState("knowledge")
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<"all" | "normal" | "pinned" | "archived">("all")
  const sessions = useQuery({queryKey: ["chat-sessions", query, filter], queryFn: () => listChatSessions(query, filter)})
  const resources = useQuery({queryKey: ["resource-workspace"], queryFn: listResourceWorkspace})
  const incidents = useQuery({queryKey: ["incidents"], queryFn: listIncidents})
  const session = useQuery({
    queryKey: ["chat-session", sessionId],
    queryFn: () => getChatSession(sessionId ?? ""),
    enabled: Boolean(sessionId),
  })
  const attachments = useQuery({
    queryKey: ["chat-attachments", sessionId],
    queryFn: () => listChatAttachments(sessionId ?? ""),
    enabled: Boolean(sessionId),
  })
  const refresh = (value: ChatSession) => {
    queryClient.setQueryData(["chat-session", value.id], value)
    queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
  }
  const create = useMutation({
    mutationFn: createChatSession,
    onSuccess: (value) => { refresh(value); navigate(`/chat/${value.id}`) },
  })
  const send = useMutation({
    mutationFn: ({content, scope, attachmentIds}: {content: string; scope?: ChatScopeSelection; attachmentIds: string[]}) => sendChatMessage(sessionId ?? "", content, undefined, scope, attachmentIds),
    onMutate: ({content}) => setPendingContent(content),
    onSuccess: refresh,
    onSettled: () => { setPendingContent(null); queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}) },
  })
  const uploadAttachment = useMutation({
    mutationFn: async (file: File) => {
      const reserved = await reserveChatAttachment(sessionId ?? "", file)
      queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]})
      return uploadChatAttachment(sessionId ?? "", reserved.id, file)
    },
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]}),
  })
  const removeAttachment = useMutation({
    mutationFn: (attachmentId: string) => deleteChatAttachment(sessionId ?? "", attachmentId),
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]}),
  })
  const retryAttachment = useMutation({
    mutationFn: (attachmentId: string) => retryChatAttachment(sessionId ?? "", attachmentId),
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]}),
  })
  const retry = useMutation({
    mutationFn: (messageId: string) => retryChatMessage(sessionId ?? "", messageId),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })
  const edit = useMutation({
    mutationFn: ({messageId, content}: {messageId: string; content: string}) => editChatMessage(sessionId ?? "", messageId, content),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })
  const reload = useMutation({
    mutationFn: (messageId: string) => reloadChatMessage(sessionId ?? "", messageId),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })
  const switchBranch = useMutation({
    mutationFn: (messageId: string) => switchChatBranch(sessionId ?? "", messageId),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })
  const update = useMutation({
    mutationFn: ({sessionId: id, changes}: {sessionId: string; changes: {title?: string; pinned?: boolean; archived?: boolean}}) =>
      updateChatSession(id, {idempotency_key: newClientId(), ...changes}),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-sessions"]}),
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteChatSession(id),
    onSuccess: (_value, id) => {
      queryClient.removeQueries({queryKey: ["chat-session", id]})
      queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
      if (sessionId === id) navigate("/chat")
    },
  })
  const handoff = useMutation({
    mutationFn: ({messageIds, target}: {messageIds: string[]; target: ChatHandoffTarget}) => createChatHandoff(sessionId ?? "", messageIds, target),
    onSuccess: () => {
      queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]})
      queryClient.invalidateQueries({queryKey: ["incidents"]})
    },
  })

  useEffect(() => {
    setSelectedTargetId(session.data?.selected_scope?.selection.deployment_target_id ?? "knowledge")
  }, [sessionId, session.data?.selected_scope?.revision])

  useEffect(() => {
    if (!sessionId) return
    setConnection("connecting")
    const cursor = session.data?.event_cursor ?? 0
    const source = new EventSource(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/events/stream?after=${cursor}`)
    source.onopen = () => setConnection("connected")
    source.onerror = () => setConnection("reconnecting")
    source.addEventListener("chat", () => {
      queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]})
      queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
    })
    return () => source.close()
  }, [queryClient, sessionId, session.data?.event_cursor])

  const failure = handoff.error ?? create.error ?? send.error ?? retry.error ?? edit.error ?? reload.error ?? switchBranch.error ?? update.error ?? remove.error ?? uploadAttachment.error ?? removeAttachment.error ?? retryAttachment.error ?? session.error ?? sessions.error ?? attachments.error
  const handoffErrors: Record<string, string> = {
    forbidden: "无权把 AI 对话转交到事件调查。",
    handoff_target_not_found: "目标 Incident 不存在或无权访问。",
    resource_not_bound: "所选资源未绑定到有效 Service。",
    investigation_terminal: "目标 Investigation 已结束，不能接收 Human Input。",
    chat_message_not_found: "所选 AI 对话消息不存在或尚未完成。",
  }
  const error = failure instanceof ApiError
    ? handoffErrors[failure.code] ?? (failure.status === 404 ? "AI 对话会话不存在或无权访问。" : failure.message)
    : failure ? "AI 对话暂时不可用。" : null
  return (
    <ChatView
      sessions={sessions.data ?? []}
      session={session.data ?? null}
      pendingContent={pendingContent}
      connection={connection}
      resources={resources.data?.resources ?? []}
      incidents={incidents.data ?? []}
      selectedTargetId={selectedTargetId}
      actionBusy={update.isPending || remove.isPending}
      query={query}
      filter={filter}
      busy={create.isPending || send.isPending || retry.isPending || edit.isPending || reload.isPending || switchBranch.isPending || handoff.isPending || update.isPending || remove.isPending || uploadAttachment.isPending || removeAttachment.isPending || retryAttachment.isPending}
      attachments={attachments.data ?? []}
      attachmentBusy={uploadAttachment.isPending || removeAttachment.isPending || retryAttachment.isPending}
      error={error}
      handoff={handoff.data && handoff.data.chat_session_id === sessionId ? handoff.data : null}
      onCreate={() => create.mutate()}
      onSelect={(id) => navigate(`/chat/${id}`)}
      onQueryChange={setQuery}
      onFilterChange={setFilter}
      onRename={(id, title) => update.mutate({sessionId: id, changes: {title}})}
      onPin={(id, pinned) => update.mutate({sessionId: id, changes: {pinned}})}
      onArchive={(id, archived) => update.mutate({sessionId: id, changes: {archived}})}
      onDelete={(id) => remove.mutate(id)}
      onSend={(content, attachmentIds) => {
        const resource = resources.data?.resources.find((item) => item.id === selectedTargetId)
        send.mutate({content, attachmentIds, scope: resource ? {cluster_id: resource.cluster_id, deployment_target_id: resource.id} : undefined})
      }}
      onFiles={(files) => Array.from(files).forEach((file) => uploadAttachment.mutate(file))}
      onRemoveAttachment={(attachmentId) => removeAttachment.mutate(attachmentId)}
      onRetryAttachment={(attachmentId) => retryAttachment.mutate(attachmentId)}
      onScopeChange={setSelectedTargetId}
      onRetry={(messageId) => retry.mutate(messageId)}
      onEdit={(messageId, content) => edit.mutate({messageId, content})}
      onReload={(messageId) => reload.mutate(messageId)}
      onSwitchBranch={(messageId) => switchBranch.mutate(messageId)}
      onHandoff={(messageIds, target) => handoff.mutate({messageIds, target})}
    />
  )
}
