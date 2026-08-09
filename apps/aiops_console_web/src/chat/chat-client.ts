import type { components } from "@/api/schema"
import { newClientId, request, write } from "@/api/transport"

export type ChatSession = components["schemas"]["ChatSession"]
export type ChatMessage = components["schemas"]["ChatMessage"]
export type ChatSessionSummary = components["schemas"]["ChatSessionSummary"]
export type ChatEvent = components["schemas"]["ChatEvent"]
export type ChatScopeSelection = components["schemas"]["ChatScopeSelection"]
export type ChatHandoff = components["schemas"]["ChatHandoff"]
export type ChatHandoffTarget = components["schemas"]["ChatHandoffRequest"]["target"]
export type ChatAttachment = components["schemas"]["ChatAttachment"]
export type ChatSessionFilter = "all" | "normal" | "pinned" | "archived"

export function listChatSessions(query = "", filter: ChatSessionFilter = "all") {
  const params = new URLSearchParams()
  if (query.trim()) params.set("query", query.trim())
  if (filter !== "all") params.set("filter", filter)
  const suffix = params.toString() ? `?${params.toString()}` : ""
  return request<components["schemas"]["ChatSessionListResponse"]>(`/api/v1/chat/sessions${suffix}`)
    .then((response) => response.chat_sessions)
}

export function createChatSession() {
  return write<components["schemas"]["ChatSessionResponse"]>(
    "/api/v1/chat/sessions", "POST", {idempotency_key: newClientId()},
  ).then((response) => response.chat_session)
}

export function getChatSession(sessionId: string) {
  return request<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`,
  ).then((response) => response.chat_session)
}

export function updateChatSession(sessionId: string, body: components["schemas"]["ChatSessionUpdateRequest"]) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`,
    "PATCH",
    body,
  ).then((response) => response.chat_session)
}

export function deleteChatSession(sessionId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatSessionDeleteResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`,
    "DELETE",
    {idempotency_key: idempotencyKey},
  )
}

export function sendChatMessage(
  sessionId: string,
  content: string,
  idempotencyKey: string = newClientId(),
  scope?: ChatScopeSelection,
  attachmentIds: string[] = [],
  signal?: AbortSignal,
) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    "POST",
    {content, idempotency_key: idempotencyKey, ...(scope ? {scope} : {}), ...(attachmentIds.length ? {attachment_ids: attachmentIds} : {})},
    undefined,
    signal,
  ).then((response) => response.chat_session)
}

export function cancelChatMessage(sessionId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/cancel`,
    "POST",
    {idempotency_key: idempotencyKey},
  ).then((response) => response.chat_session)
}

export function listChatAttachments(sessionId: string) {
  return request<components["schemas"]["ChatAttachmentListResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments`,
  ).then((response) => response.attachments)
}

export function reserveChatAttachment(sessionId: string, file: File, idempotencyKey: string = newClientId()) {
  const extension = file.name.toLowerCase().split(".").pop() ?? ""
  const fallbackMime: Record<string, string> = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", pdf: "application/pdf", txt: "text/plain", log: "text/plain", md: "text/markdown", markdown: "text/markdown", json: "application/json", yaml: "application/yaml", yml: "application/yaml", csv: "text/csv"}
  return write<components["schemas"]["ChatAttachmentResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments`,
    "POST",
    {filename: file.name, content_type: file.type || fallbackMime[extension] || "application/octet-stream", size: file.size, idempotency_key: idempotencyKey},
  ).then((response) => response.attachment)
}

export async function uploadChatAttachment(sessionId: string, attachmentId: string, file: File, idempotencyKey: string = newClientId()) {
  const {csrf_token} = await request<components["schemas"]["CsrfResponse"]>("/auth/csrf")
  return request<components["schemas"]["ChatAttachmentResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}/content`,
    {method: "PUT", headers: {"Content-Type": "application/octet-stream", "X-CSRF-Token": csrf_token, "X-Idempotency-Key": idempotencyKey}, body: file},
  ).then((response) => response.attachment)
}

export function deleteChatAttachment(sessionId: string, attachmentId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatAttachmentDeleteResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}`,
    "DELETE",
    {idempotency_key: idempotencyKey},
  )
}

export function retryChatAttachment(sessionId: string, attachmentId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatAttachmentResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}/retry`,
    "POST",
    {idempotency_key: idempotencyKey},
  ).then((response) => response.attachment)
}

export function chatAttachmentDownloadUrl(sessionId: string, attachmentId: string) {
  return `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}/download`
}

export function retryChatMessage(sessionId: string, messageId: string) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/retry`,
    "POST",
    {},
  ).then((response) => response.chat_session)
}

export function editChatMessage(sessionId: string, messageId: string, content: string, scope?: ChatScopeSelection, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/edit`,
    "POST",
    {content, idempotency_key: idempotencyKey, ...(scope ? {scope} : {})},
  ).then((response) => response.chat_session)
}

export function reloadChatMessage(sessionId: string, messageId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/reload`,
    "POST",
    {idempotency_key: idempotencyKey},
  ).then((response) => response.chat_session)
}

export function switchChatBranch(sessionId: string, messageId: string, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/branches`,
    "POST",
    {message_id: messageId, idempotency_key: idempotencyKey},
  ).then((response) => response.chat_session)
}

export function createChatHandoff(sessionId: string, messageIds: string[], target: ChatHandoffTarget, idempotencyKey: string = newClientId()) {
  return write<components["schemas"]["ChatHandoffResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/handoffs`,
    "POST",
    {message_ids: messageIds, idempotency_key: idempotencyKey, target},
  ).then((response) => response.handoff)
}
