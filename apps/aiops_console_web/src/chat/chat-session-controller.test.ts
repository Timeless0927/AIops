import { beforeEach, describe, expect, it, vi } from "vitest"

const reactState = vi.hoisted(() => ({index: 0, values: [] as unknown[]}))
const mutationState = vi.hoisted(() => ({index: 0, errors: [] as unknown[]}))

vi.mock("react", async (importOriginal) => ({
  ...await importOriginal<typeof import("react")>(),
  useEffect: () => undefined,
  useRef: <T>(value: T) => ({current: value}),
  useState: <T>(value: T) => {
    const index = reactState.index++
    if (!(index in reactState.values)) reactState.values[index] = value
    return [reactState.values[index] as T, (next: T) => { reactState.values[index] = next }] as const
  },
}))

vi.mock("@tanstack/react-query", () => ({
  useMutation: vi.fn(),
  useQuery: vi.fn(),
  useQueryClient: vi.fn(),
}))

vi.mock("@/chat/chat-client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/chat/chat-client")>(),
  cancelChatMessage: vi.fn(),
  createChatHandoff: vi.fn(),
  createChatSession: vi.fn(),
  deleteChatAttachment: vi.fn(),
  deleteChatSession: vi.fn(),
  editChatMessage: vi.fn(),
  getChatSession: vi.fn(),
  listChatAttachments: vi.fn(),
  listChatSessions: vi.fn(),
  reloadChatMessage: vi.fn(),
  reserveChatAttachment: vi.fn(),
  retryChatAttachment: vi.fn(),
  retryChatMessage: vi.fn(),
  sendChatMessage: vi.fn(),
  switchChatBranch: vi.fn(),
  updateChatSession: vi.fn(),
  uploadChatAttachment: vi.fn(),
}))

vi.mock("@/incidents/incident-client", () => ({listIncidents: vi.fn()}))
vi.mock("@/resources/resource-client", () => ({listResourceWorkspace: vi.fn()}))

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  cancelChatMessage,
  createChatSession,
  deleteChatAttachment,
  deleteChatSession,
  editChatMessage,
  reloadChatMessage,
  reserveChatAttachment,
  retryChatAttachment,
  retryChatMessage,
  sendChatMessage,
  uploadChatAttachment,
  type ChatAttachment,
  type ChatSession,
} from "@/chat/chat-client"
import { useChatSessionController } from "@/chat/chat-session-controller"

const session: ChatSession = {
  id: "chat-1", title: "新对话", created_at: 1, updated_at: 1, expires_at: null, message_count: 0,
  selected_scope: null, pinned: false, archived: false, title_manual: false, event_cursor: 0,
  current_branch_head_id: null, messages: [],
}

const readyAttachment: ChatAttachment = {
  id: "attachment-1", session_id: "chat-1", filename: "incident.log", content_type: "text/plain", size: 3,
  sha256: "a".repeat(64), status: "ready", parse_state: "ready", extraction_sha256: "a".repeat(64),
  model_use_status: "not_used", rejection_code: null, message_id: null, created_at: 1, updated_at: 2,
}

function setup(attachments: ChatAttachment[]) {
  const queryClient = {
    getQueryData: vi.fn(),
    setQueryData: vi.fn(),
    invalidateQueries: vi.fn(),
    removeQueries: vi.fn(),
  }
  vi.mocked(useQueryClient).mockReturnValue(queryClient as never)
  vi.mocked(useQuery).mockImplementation((({queryKey}: {queryKey: readonly unknown[]}) => {
    const key = queryKey[0]
    const data = key === "chat-sessions" ? [session]
      : key === "chat-session" ? session
        : key === "chat-attachments" ? attachments
          : key === "resource-workspace" ? {resources: []}
            : []
    return {data, error: null, isLoading: false}
  }) as never)
  vi.mocked(useMutation).mockImplementation(((options: {
    mutationFn: (variables?: never) => unknown
    onMutate?: (variables: never) => void
    onError?: (error: unknown, variables: never) => void
    onSuccess?: (value: unknown, variables: never) => void
    onSettled?: () => void
  }) => {
    const index = mutationState.index++
    const run = async (variables?: never) => {
      mutationState.errors[index] = null
      options.onMutate?.(variables as never)
      try {
        const value = await options.mutationFn(variables)
        options.onSuccess?.(value, variables as never)
        return value
      } catch (error) {
        mutationState.errors[index] = error
        options.onError?.(error, variables as never)
        throw error
      } finally {
        options.onSettled?.()
      }
    }
    return {
      data: undefined,
      error: mutationState.errors[index] ?? null,
      isPending: false,
      mutate: (variables?: never) => { void run(variables) },
      mutateAsync: run,
      reset: vi.fn(),
    }
  }) as never)
  return queryClient
}

function renderController() {
  reactState.index = 0
  mutationState.index = 0
  return useChatSessionController({sessionId: "chat-1", query: "", filter: "all"})
}

describe("useChatSessionController", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    reactState.index = 0
    reactState.values = []
    mutationState.index = 0
    mutationState.errors = []
    vi.mocked(createChatSession).mockResolvedValue(session)
    vi.mocked(deleteChatSession).mockResolvedValue({request_id: "delete"} as never)
    vi.mocked(cancelChatMessage).mockResolvedValue(session)
    vi.mocked(retryChatMessage).mockResolvedValue(session)
    vi.mocked(editChatMessage).mockResolvedValue(session)
    vi.mocked(reloadChatMessage).mockResolvedValue(session)
    vi.mocked(sendChatMessage).mockResolvedValue(session)
    vi.mocked(deleteChatAttachment).mockResolvedValue({request_id: "delete-attachment"} as never)
    vi.mocked(reserveChatAttachment).mockResolvedValue({...readyAttachment, status: "pending", parse_state: "pending"})
    vi.mocked(uploadChatAttachment).mockResolvedValue(readyAttachment)
    vi.mocked(retryChatAttachment).mockResolvedValue(readyAttachment)
  })

  it("returns create/remove results and routes message lifecycle through the current session", async () => {
    setup([])
    const controller = renderController()

    await expect(controller.actions.create()).resolves.toBe(session)
    await expect(controller.actions.remove("chat-1")).resolves.toBe("chat-1")
    controller.actions.cancel()
    controller.actions.retry("assistant-failed")
    controller.actions.edit("user-1", "修订问题")
    controller.actions.reload("assistant-1")

    expect(cancelChatMessage).toHaveBeenCalledWith("chat-1")
    expect(retryChatMessage).toHaveBeenCalledWith("chat-1", "assistant-failed")
    expect(editChatMessage).toHaveBeenCalledWith("chat-1", "user-1", "修订问题")
    expect(reloadChatMessage).toHaveBeenCalledWith("chat-1", "assistant-1")
  })

  it("owns attachment readiness and the reserve/upload/retry/remove lifecycle", async () => {
    const pending = {...readyAttachment, status: "pending" as const, parse_state: "pending" as const}
    setup([readyAttachment, pending])
    const blocked = renderController()
    expect(blocked.state.attachmentPending).toBe(true)
    blocked.actions.send("不会发送")
    expect(sendChatMessage).not.toHaveBeenCalled()

    setup([readyAttachment])
    const controller = renderController()
    expect(controller.state.attachmentPending).toBe(false)
    controller.actions.send("检查日志")
    expect(sendChatMessage).toHaveBeenCalledWith("chat-1", "检查日志", undefined, undefined, ["attachment-1"], expect.any(AbortSignal))

    const addition = controller.attachmentAdapter!.add({file: new File(["log"], "incident.log", {type: "text/plain"})})
    if (!(Symbol.asyncIterator in addition)) throw new Error("expected attachment lifecycle")
    for await (const _state of addition) { /* consume lifecycle */ }
    await controller.actions.retryAttachment("attachment-1")
    await controller.actions.removeAttachment("attachment-1")

    expect(reserveChatAttachment).toHaveBeenCalledWith("chat-1", expect.any(File))
    expect(uploadChatAttachment).toHaveBeenCalledWith("chat-1", "attachment-1", expect.any(File))
    expect(retryChatAttachment).toHaveBeenCalledWith("chat-1", "attachment-1")
    expect(deleteChatAttachment).toHaveBeenCalledWith("chat-1", "attachment-1")
  })

  it("clears an upload failure after retrying the attachment successfully", async () => {
    const uploadFailure = new Error("upload failed")
    setup([])
    vi.mocked(uploadChatAttachment).mockRejectedValueOnce(uploadFailure)
    let controller = renderController()
    const addition = controller.attachmentAdapter!.add({file: new File(["log"], "incident.log", {type: "text/plain"})})
    if (!(Symbol.asyncIterator in addition)) throw new Error("expected attachment lifecycle")

    await expect((async () => { for await (const _state of addition) { /* consume lifecycle */ } })()).rejects.toBe(uploadFailure)
    controller = renderController()
    expect(controller.errors.attachment).toBe(uploadFailure)

    await controller.actions.retryAttachment("attachment-1")
    controller = renderController()
    expect(controller.errors.attachment).toBeNull()
  })
})
