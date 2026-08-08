import { ExportedMessageRepository } from "@assistant-ui/react"
import type {
  ExternalStoreThreadData,
  ExternalStoreThreadListAdapter,
} from "@assistant-ui/react"

import type { ChatMessage, ChatSession } from "@/api/client"
import type { ChatSessionSummary } from "@/api/client"

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
    onSwitchToNewThread: onCreate,
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
