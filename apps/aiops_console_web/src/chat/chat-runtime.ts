import {
  ExportedMessageRepository,
  type AttachmentAdapter,
  type CompleteAttachment,
  type PendingAttachment,
} from "@assistant-ui/react"
import type {
  ExternalStoreThreadData,
  ExternalStoreThreadListAdapter,
} from "@assistant-ui/react"

import { chatAttachmentDownloadUrl, type ChatAttachment, type ChatEvent, type ChatMessage, type ChatSession, type ChatSessionSummary } from "@/chat/chat-client"

export const CHAT_ATTACHMENT_ACCEPT = ".png,.jpg,.jpeg,.webp,.pdf,.txt,.log,.md,.markdown,.json,.yaml,.yml,.csv"

export type ChatAttachmentAdapter = AttachmentAdapter & {
  retry: (attachmentId: string) => Promise<void>
}

function runtimeAttachment(attachment: ChatAttachment): CompleteAttachment {
  const image = attachment.content_type.startsWith("image/")
  const url = chatAttachmentDownloadUrl(attachment.session_id, attachment.id)
  return {
    id: attachment.id,
    type: image ? "image" : "document",
    name: attachment.filename,
    contentType: attachment.content_type,
    status: {type: "complete"},
    content: image
      ? [{type: "image", image: url, filename: attachment.filename}]
      : [{type: "file", data: url, mimeType: attachment.content_type, filename: attachment.filename, sourceType: "url"}],
  }
}

export function chatAttachmentAdapter({
  reserve,
  upload,
  retry,
  remove,
  onChange,
  onError,
}: {
  reserve: (file: File) => Promise<ChatAttachment>
  upload: (attachmentId: string, file: File) => Promise<ChatAttachment>
  retry: (attachmentId: string) => Promise<ChatAttachment>
  remove: (attachmentId: string) => Promise<void>
  onChange: (attachment: ChatAttachment) => void
  onError?: (error: unknown | null) => void
}): ChatAttachmentAdapter {
  return {
    accept: CHAT_ATTACHMENT_ACCEPT,
    async *add({file}) {
      onError?.(null)
      let pending: PendingAttachment | undefined
      try {
        const reserved = await reserve(file)
        onChange(reserved)
        pending = {
          id: reserved.id,
          type: reserved.content_type.startsWith("image/") ? "image" : "document",
          name: reserved.filename,
          contentType: reserved.content_type,
          file,
          status: {type: "running", reason: "uploading", progress: 0},
        }
        yield pending
        const uploaded = await upload(reserved.id, file)
        onChange(uploaded)
        yield {
          ...pending,
          status: uploaded.status === "rejected" || uploaded.status === "failed"
            ? {type: "incomplete", reason: "error", message: uploaded.rejection_code ?? "附件处理失败"}
            : {type: "requires-action", reason: "composer-send"},
        }
      } catch (error) {
        onError?.(error)
        if (pending) yield {...pending, status: {type: "incomplete", reason: "error", message: error instanceof Error ? error.message : "附件上传失败"}}
        throw error
      }
    },
    async remove(attachment) {
      try {
        await remove(attachment.id)
      } catch (error) {
        onError?.(error)
        throw error
      }
    },
    async retry(attachmentId) {
      onError?.(null)
      try {
        onChange(await retry(attachmentId))
      } catch (error) {
        onError?.(error)
        throw error
      }
    },
    async send(attachment) {
      return {...attachment, status: {type: "complete"}, content: []}
    },
  }
}

export type GatewayMessageMetadata = {
  status: ChatMessage["status"]
  errorCode: ChatMessage["error_code"]
  mode: ChatMessage["mode"]
  scope: ChatMessage["scope"]
  toolActivity: ChatMessage["tool_activity"]
  evidenceReferences: ChatMessage["evidence_references"]
  uncertainty: ChatMessage["uncertainty"]
  nextStep: ChatMessage["next_step"]
  completion: ChatMessage["completion"]
  skillVersions: ChatMessage["skill_versions"]
  attachments: ChatMessage["attachments"]
}

export function gatewayMessageMetadata(message: ChatMessage): GatewayMessageMetadata {
  return {
    status: message.status,
    errorCode: message.error_code,
    mode: message.mode,
    scope: message.scope,
    toolActivity: message.tool_activity,
    evidenceReferences: message.evidence_references,
    uncertainty: message.uncertainty,
    nextStep: message.next_step,
    completion: message.completion,
    skillVersions: message.skill_versions,
    attachments: message.attachments,
  }
}

export function chatMessageRepository(session: ChatSession | null) {
  return ExportedMessageRepository.fromBranchableArray(
    (session?.messages ?? []).map((message) => ({
      parentId: message.parent_id,
      message: {
        id: message.id,
        role: message.role,
        content: message.content,
        createdAt: new Date(message.created_at * 1000),
        metadata: {custom: {gateway: gatewayMessageMetadata(message)}},
        ...(message.role === "user" ? {attachments: (message.attachments ?? []).map(runtimeAttachment)} : {}),
        ...(message.role === "assistant" ? {
          status: message.status === "sending"
            ? {type: "running" as const}
            : message.status === "failed"
              ? {type: "incomplete" as const, reason: "error" as const}
              : {type: "complete" as const, reason: "stop" as const},
        } : {}),
      },
    })),
    {headId: session?.current_branch_head_id ?? null},
  )
}

export function applyChatEvent(session: ChatSession | undefined, event: ChatEvent): ChatSession | undefined {
  if (!session || session.id !== event.session_id || event.id <= session.event_cursor) return session
  if (event.type !== "message.delta") return {...session, event_cursor: event.id}
  const messageId = event.payload.message_id
  const delta = event.payload.delta
  if (typeof messageId !== "string" || typeof delta !== "string") return session
  return {
    ...session,
    event_cursor: event.id,
    messages: session.messages.map((message) => message.id === messageId
      ? {...message, status: "sending", content: message.content + delta, updated_at: event.created_at}
      : message),
  }
}

export function textFromAssistantMessage(content: string | readonly {type: string; text?: string}[]) {
  return typeof content === "string"
    ? content
    : content.map((part) => part.type === "text" ? part.text ?? "" : "").join("")
}

type ChatThreadListAdapterOptions = {
  threadId?: string
  sessions: readonly ChatSessionSummary[]
  archived: boolean
  onCreate: () => Promise<void> | void
  onSelect: (sessionId: string) => Promise<void> | void
  onRename: (sessionId: string, title: string) => Promise<void> | void
  onPin: (sessionId: string, pinned: boolean) => Promise<void> | void
  onArchive: (sessionId: string, archived: boolean) => Promise<void> | void
  onDelete: (sessionId: string) => Promise<void> | void
}

export function chatThreadListAdapter({
  threadId,
  sessions,
  archived,
  onCreate,
  onSelect,
  onRename,
  onPin,
  onArchive,
  onDelete,
}: ChatThreadListAdapterOptions): ExternalStoreThreadListAdapter {
  const threads: ExternalStoreThreadData<"regular">[] = sessions.map((session) => ({
    id: session.id,
    status: "regular",
    title: session.title,
    custom: {pinned: session.pinned, archived: session.archived, messageCount: session.message_count},
  }))

  return {
    threadId,
    threads: archived ? [] : threads,
    archivedThreads: archived ? sessions.map((session) => ({...threads.find((item) => item.id === session.id)!, status: "archived" as const})) : [],
    onSwitchToNewThread: () => {
      const active = threadId ? sessions.find((session) => session.id === threadId) : undefined
      const reusable = active && !active.archived && active.message_count === 0
        ? active
        : sessions.find((session) => !session.archived && session.message_count === 0)
      if (reusable) return reusable.id === threadId ? undefined : onSelect(reusable.id)
      return onCreate()
    },
    onSwitchToThread: onSelect,
    onRename,
    onUpdateCustom: (sessionId, custom) => {
      if (typeof custom?.pinned === "boolean") return onPin(sessionId, custom.pinned)
    },
    onArchive: (sessionId) => onArchive(sessionId, true),
    onUnarchive: (sessionId) => onArchive(sessionId, false),
    onDelete,
  }
}
