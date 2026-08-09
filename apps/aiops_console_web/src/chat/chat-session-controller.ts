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
  retryChatMessage,
  sendChatMessage,
  switchChatBranch,
  updateChatSession,
  type ChatAttachment,
  type ChatEvent,
  type ChatHandoffTarget,
  type ChatScopeSelection,
  type ChatSession,
  type ChatSessionFilter,
} from "@/chat/chat-client"
import { newClientId } from "@/api/transport"
import { applyChatEvent } from "@/chat/chat-runtime"

type ChatSessionControllerOptions = {
  sessionId?: string
  query: string
  filter: ChatSessionFilter
  onCreated: (sessionId: string) => void
  onDeleted: (sessionId: string) => void
}

export function useChatSessionController({sessionId, query, filter, onCreated, onDeleted}: ChatSessionControllerOptions) {
  const queryClient = useQueryClient()
  const sendAbort = useRef<AbortController | null>(null)
  const [pendingContent, setPendingContent] = useState<string | null>(null)
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

  const refresh = (value: ChatSession) => {
    queryClient.setQueryData(["chat-session", value.id], value)
    queryClient.invalidateQueries({queryKey: ["chat-sessions"]})
  }
  const create = useMutation({
    mutationFn: createChatSession,
    onSuccess: (value) => {
      refresh(value)
      onCreated(value.id)
    },
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
    onSuccess: (_value, attachmentId) => queryClient.setQueryData<ChatAttachment[]>(
      ["chat-attachments", sessionId],
      (current = []) => current.filter((item) => item.id !== attachmentId),
    ),
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
      onDeleted(id)
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

  const actionError = handoff.error ?? create.error ?? cancel.error ?? send.error ?? retry.error ?? edit.error ?? reload.error ?? switchBranch.error ?? update.error ?? remove.error
  const generating = send.isPending || retry.isPending || edit.isPending || reload.isPending || cancel.isPending || Boolean(session.data?.messages.some((message) => message.status === "sending"))
  const busy = create.isPending || send.isPending || retry.isPending || edit.isPending || reload.isPending || cancel.isPending || switchBranch.isPending || handoff.isPending || update.isPending || remove.isPending || removeAttachment.isPending

  return {
    state: {
      sessions: sessions.data ?? [],
      session: session.data ?? null,
      attachments: attachments.data ?? [],
      pendingContent,
      connection,
      handoff: handoff.data && handoff.data.chat_session_id === sessionId ? handoff.data : null,
      generating,
      loading: sessions.isLoading || (Boolean(sessionId) && session.isLoading),
      busy,
      actionBusy: update.isPending || remove.isPending,
      attachmentBusy: removeAttachment.isPending,
    },
    errors: {
      action: actionError,
      attachment: removeAttachment.error,
      query: session.error ?? sessions.error ?? attachments.error,
    },
    actions: {
      create: () => create.mutate(),
      send: (content: string, scope: ChatScopeSelection | undefined, attachmentIds: string[]) => send.mutate({content, scope, attachmentIds}),
      cancel: () => { if (!cancel.isPending) cancel.mutate() },
      retry: (messageId: string) => retry.mutate(messageId),
      edit: (messageId: string, content: string) => edit.mutate({messageId, content}),
      reload: (messageId: string) => reload.mutate(messageId),
      switchBranch: (messageId: string) => switchBranch.mutate(messageId),
      update: (id: string, changes: {title?: string; pinned?: boolean; archived?: boolean}) => update.mutate({sessionId: id, changes}),
      remove: (id: string) => remove.mutate(id),
      handoff: (messageIds: string[], target: ChatHandoffTarget) => handoff.mutate({messageIds, target}),
      dismissHandoff: () => handoff.reset(),
      removeAttachment: (attachmentId: string) => removeAttachment.mutate(attachmentId),
      deleteAttachment: (attachmentId: string) => removeAttachment.mutateAsync(attachmentId).then(() => undefined),
      updateAttachment: (value: ChatAttachment) => queryClient.setQueryData<ChatAttachment[]>(
        ["chat-attachments", sessionId],
        (current = []) => [...current.filter((item) => item.id !== value.id), value],
      ),
      refreshAttachments: () => queryClient.invalidateQueries({queryKey: ["chat-attachments", sessionId]}),
    },
  }
}
