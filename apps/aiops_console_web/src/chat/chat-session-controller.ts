import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  cancelChatMessage,
  createChatHandoff,
  createChatSession,
  deleteChatAttachment,
  deleteChatSession,
  editChatMessage,
  getChatSession,
  listChatAttachments,
  listChatSessions,
  reloadChatMessage,
  reserveChatAttachment,
  retryChatAttachment,
  retryChatMessage,
  sendChatMessage,
  switchChatBranch,
  updateChatSession,
  uploadChatAttachment,
  type ChatAttachment,
  type ChatEvent,
  type ChatHandoff,
  type ChatHandoffTarget,
  type ChatScopeSelection,
  type ChatSession,
  type ChatSessionFilter,
  type ChatSessionSummary,
} from "@/chat/chat-client"
import { newClientId } from "@/api/transport"
import { applyChatEvent, chatAttachmentAdapter, type ChatAttachmentAdapter } from "@/chat/chat-runtime"
import { listIncidents, type Incident } from "@/incidents/incident-client"
import { listResourceWorkspace, type ResourceWorkspace } from "@/resources/resource-client"

type ChatSessionControllerOptions = {
  sessionId?: string
  query: string
  filter: ChatSessionFilter
}

export type ChatSessionController = {
  state: {
    sessions: ChatSessionSummary[]
    session: ChatSession | null
    attachments: ChatAttachment[]
    attachmentPending: boolean
    pendingContent: string | null
    connection: "connecting" | "connected" | "reconnecting"
    handoff: ChatHandoff | null
    resources: ResourceWorkspace["resources"]
    incidents: Incident[]
    generating: boolean
    loading: boolean
    busy: boolean
    actionBusy: boolean
    attachmentBusy: boolean
  }
  errors: {action: unknown; attachment: unknown; query: unknown}
  attachmentAdapter?: ChatAttachmentAdapter
  actions: {
    create: () => Promise<ChatSession>
    send: (content: string, scope?: ChatScopeSelection) => void
    cancel: () => void
    retry: (messageId: string) => void
    edit: (messageId: string, content: string) => void
    reload: (messageId: string) => void
    switchBranch: (messageId: string) => void
    update: (sessionId: string, changes: {title?: string; pinned?: boolean; archived?: boolean}) => void
    remove: (sessionId: string) => Promise<string>
    handoff: (messageIds: string[], target: ChatHandoffTarget) => void
    dismissHandoff: () => void
    removeAttachment: (attachmentId: string) => Promise<void>
    retryAttachment: (attachmentId: string) => Promise<void>
  }
}

export function useChatSessionController({sessionId, query, filter}: ChatSessionControllerOptions): ChatSessionController {
  const queryClient = useQueryClient()
  const sendAbort = useRef<AbortController | null>(null)
  const [pendingContent, setPendingContent] = useState<string | null>(null)
  const [attachmentFailure, setAttachmentFailure] = useState<unknown | null>(null)
  const [connection, setConnection] = useState<"connecting" | "connected" | "reconnecting">("connecting")
  const sessions = useQuery({queryKey: ["chat-sessions", query, filter], queryFn: () => listChatSessions(query, filter)})
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
  const resources = useQuery({queryKey: ["resource-workspace"], queryFn: listResourceWorkspace})
  const incidents = useQuery({queryKey: ["incidents"], queryFn: listIncidents})
  const updateAttachment = (value: ChatAttachment) => queryClient.setQueryData<ChatAttachment[]>(
    ["chat-attachments", sessionId],
    (current = []) => [...current.filter((item) => item.id !== value.id), value],
  )

  const refresh = (value: ChatSession) => {
    queryClient.setQueryData(["chat-session", value.id], value)
    queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
  }
  const create = useMutation({
    mutationFn: createChatSession,
    onSuccess: refresh,
  })
  const send = useMutation({
    mutationFn: ({content, scope, attachmentIds}: {content: string; scope?: ChatScopeSelection; attachmentIds: string[]}) => {
      sendAbort.current = new AbortController()
      return sendChatMessage(sessionId ?? "", content, undefined, scope, attachmentIds, sendAbort.current.signal)
    },
    onMutate: ({content}) => setPendingContent(content),
    onSuccess: refresh,
    onSettled: () => {
      sendAbort.current = null
      setPendingContent(null)
      queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]})
      queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]})
    },
  })
  const cancel = useMutation({
    mutationFn: () => cancelChatMessage(sessionId ?? ""),
    onSuccess: (value) => {
      sendAbort.current?.abort()
      send.reset()
      refresh(value)
    },
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })
  const removeAttachment = useMutation({
    mutationFn: (attachmentId: string) => deleteChatAttachment(sessionId ?? "", attachmentId),
    onMutate: () => setAttachmentFailure(null),
    onError: setAttachmentFailure,
    onSuccess: (_value, attachmentId) => queryClient.setQueryData<ChatAttachment[]>(
      ["chat-attachments", sessionId],
      (current = []) => current.filter((item) => item.id !== attachmentId),
    ),
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]}),
  })
  const reserveAttachment = useMutation({mutationFn: (file: File) => reserveChatAttachment(sessionId ?? "", file)})
  const uploadAttachment = useMutation({mutationFn: ({attachmentId, file}: {attachmentId: string; file: File}) => uploadChatAttachment(sessionId ?? "", attachmentId, file)})
  const retryAttachment = useMutation({
    mutationFn: (attachmentId: string) => retryChatAttachment(sessionId ?? "", attachmentId),
    onMutate: () => setAttachmentFailure(null),
    onError: setAttachmentFailure,
    onSuccess: (value) => {
      updateAttachment(value)
      setAttachmentFailure(null)
    },
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
    if (!sessionId) return
    setConnection("connecting")
    const cursor = queryClient.getQueryData<ChatSession>(["chat-session", sessionId])?.event_cursor ?? 0
    const source = new EventSource(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/events/stream?after=${cursor}`)
    source.onopen = () => setConnection("connected")
    source.onerror = () => setConnection("reconnecting")
    source.addEventListener("chat", (message) => {
      let event: ChatEvent | undefined
      try {
        event = JSON.parse((message as MessageEvent<string>).data) as ChatEvent
      } catch {
        event = undefined
      }
      if (event?.type === "message.delta") {
        const current = queryClient.getQueryData<ChatSession>(["chat-session", sessionId])
        if (current?.messages.some((item) => item.id === event.payload.message_id)) {
          queryClient.setQueryData(["chat-session", sessionId], applyChatEvent(current, event))
          return
        }
      }
      queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]})
      queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
      queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]})
    })
    return () => source.close()
  }, [queryClient, sessionId])

  useEffect(() => setAttachmentFailure(null), [sessionId])

  const attachmentItems = attachments.data ?? []
  const composerAttachments = attachmentItems.filter((attachment) => !attachment.message_id)
  const readyAttachmentIds = composerAttachments.filter((attachment) => attachment.status === "ready").map((attachment) => attachment.id)
  const attachmentPending = composerAttachments.some((attachment) => attachment.status !== "ready")
  const attachmentAdapter = sessionId ? chatAttachmentAdapter({
    reserve: (file) => reserveAttachment.mutateAsync(file),
    upload: (attachmentId, file) => uploadAttachment.mutateAsync({attachmentId, file}),
    retry: (attachmentId) => retryAttachment.mutateAsync(attachmentId),
    remove: async (attachmentId) => { await removeAttachment.mutateAsync(attachmentId) },
    onChange: updateAttachment,
    onError: setAttachmentFailure,
  }) : undefined

  const actionError = handoff.error ?? create.error ?? cancel.error ?? send.error ?? retry.error ?? edit.error ?? reload.error ?? switchBranch.error ?? update.error ?? remove.error
  const generating = send.isPending || retry.isPending || edit.isPending || reload.isPending || cancel.isPending || Boolean(session.data?.messages.some((message) => message.status === "sending"))
  const busy = create.isPending || send.isPending || retry.isPending || edit.isPending || reload.isPending || cancel.isPending || switchBranch.isPending || handoff.isPending || update.isPending || remove.isPending || removeAttachment.isPending || retryAttachment.isPending

  return {
    state: {
      sessions: sessions.data ?? [],
      session: session.data ?? null,
      attachments: attachmentItems,
      attachmentPending,
      pendingContent,
      connection,
      handoff: handoff.data && handoff.data.chat_session_id === sessionId ? handoff.data : null,
      resources: resources.data?.resources ?? [],
      incidents: incidents.data ?? [],
      generating,
      loading: sessions.isLoading || (Boolean(sessionId) && session.isLoading),
      busy,
      actionBusy: update.isPending || remove.isPending,
      attachmentBusy: removeAttachment.isPending || retryAttachment.isPending,
    },
    errors: {
      action: actionError,
      attachment: attachmentFailure,
      query: session.error ?? sessions.error ?? attachments.error,
    },
    attachmentAdapter,
    actions: {
      create: () => create.mutateAsync(),
      send: (content: string, scope?: ChatScopeSelection) => { if (!attachmentPending) send.mutate({content, scope, attachmentIds: readyAttachmentIds}) },
      cancel: () => { if (!cancel.isPending) cancel.mutate() },
      retry: (messageId: string) => retry.mutate(messageId),
      edit: (messageId: string, content: string) => edit.mutate({messageId, content}),
      reload: (messageId: string) => reload.mutate(messageId),
      switchBranch: (messageId: string) => switchBranch.mutate(messageId),
      update: (id: string, changes: {title?: string; pinned?: boolean; archived?: boolean}) => update.mutate({sessionId: id, changes}),
      remove: async (id: string) => { await remove.mutateAsync(id); return id },
      handoff: (messageIds: string[], target: ChatHandoffTarget) => handoff.mutate({messageIds, target}),
      dismissHandoff: () => handoff.reset(),
      removeAttachment: async (attachmentId: string) => { await removeAttachment.mutateAsync(attachmentId) },
      retryAttachment: async (attachmentId: string) => { await retryAttachment.mutateAsync(attachmentId) },
    },
  }
}
