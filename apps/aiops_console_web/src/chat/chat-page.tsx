import { useEffect, useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useParams } from "react-router"

import {
  ApiError,
  createChatSession,
  getChatSession,
  listChatSessions,
  retryChatMessage,
  sendChatMessage,
  type ChatSession,
  type ChatSessionSummary,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"

type ChatViewProps = {
  sessions: ChatSessionSummary[]
  session: ChatSession | null
  pendingContent: string | null
  connection: "connecting" | "connected" | "reconnecting"
  busy: boolean
  error: string | null
  onCreate: () => void
  onSelect: (sessionId: string) => void
  onSend: (content: string) => void
  onRetry: (messageId: string) => void
}

export function ChatView({
  sessions,
  session,
  pendingContent,
  connection,
  busy,
  error,
  onCreate,
  onSelect,
  onSend,
  onRetry,
}: ChatViewProps) {
  const [draft, setDraft] = useState("")
  function submit(event: FormEvent) {
    event.preventDefault()
    const content = draft.trim()
    if (!content || busy || !session) return
    setDraft("")
    onSend(content)
  }

  return (
    <main className="mx-auto grid min-h-[calc(100vh-3.25rem)] max-w-[1600px] min-w-0 md:grid-cols-[18rem_1fr]">
      <aside className="min-w-0 border-b p-4 md:border-r md:border-b-0">
        <div className="flex items-center justify-between gap-3">
          <div><h1 className="text-xl font-semibold">Chat</h1><p className="mt-1 text-xs text-muted-foreground">普通对话固定保留 30 天</p></div>
          <Button size="sm" onClick={onCreate} disabled={busy}>新建对话</Button>
        </div>
        <nav aria-label="Chat 会话" className="mt-4 space-y-1">
          {sessions.map((item) => (
            <Button
              key={item.id}
              variant={item.id === session?.id ? "secondary" : "ghost"}
              className="h-auto w-full min-w-0 justify-start px-3 py-2 text-left"
              onClick={() => onSelect(item.id)}
            >
              <span className="min-w-0"><span className="block truncate">{item.title}</span><span className="block text-xs text-muted-foreground">{item.message_count} 条消息</span></span>
            </Button>
          ))}
          {!sessions.length ? <p className="py-8 text-center text-sm text-muted-foreground">尚无 Chat Session</p> : null}
        </nav>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-col" aria-label="Chat 消息">
        {session ? (
          <>
            <header className="flex items-center justify-between gap-3 border-b px-4 py-3">
              <h2 className="truncate font-medium">{session.title}</h2>
              <p className="shrink-0 text-xs text-muted-foreground" role="status" aria-live="polite">
                {connection === "connected" ? "实时更新已连接" : connection === "reconnecting" ? "连接已断开，正在恢复实时更新" : "正在连接实时更新"}
              </p>
            </header>
            <div className="flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
              {session.messages.map((message) => (
                <article
                  key={message.id}
                  className={message.role === "user" ? "ml-auto max-w-2xl rounded-lg bg-primary px-4 py-3 text-primary-foreground" : "max-w-2xl rounded-lg border bg-card px-4 py-3"}
                >
                  <div className="mb-1 flex items-center gap-2 text-xs opacity-70">
                    <span>{message.role === "user" ? "你" : "AIOps"}</span>
                    {message.status === "sending" ? <Badge variant="outline">正在回答</Badge> : null}
                    {message.status === "failed" ? <Badge variant="destructive">回答失败</Badge> : null}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm">{message.content || (message.status === "sending" ? "正在回答…" : "")}</p>
                  {message.status === "failed" ? <Button className="mt-2" size="sm" variant="outline" onClick={() => onRetry(message.id)} disabled={busy}>重试</Button> : null}
                </article>
              ))}
              {pendingContent ? <article className="ml-auto max-w-2xl rounded-lg bg-primary px-4 py-3 text-primary-foreground"><p className="whitespace-pre-wrap break-words text-sm">{pendingContent}</p><span className="mt-1 block text-xs opacity-70">正在发送</span></article> : null}
            </div>
            <form className="border-t p-4" onSubmit={submit}>
              <label htmlFor="chat-message" className="sr-only">输入消息</label>
              <Textarea
                id="chat-message"
                aria-label="输入消息"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="询问 AIOps 或 Kubernetes 知识"
                maxLength={8000}
                disabled={busy}
              />
              <div className="mt-2 flex items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">当前 Chat 仅回答知识问题，不查询实时环境。</p>
                <Button type="submit" disabled={busy || !draft.trim()}>发送</Button>
              </div>
            </form>
          </>
        ) : (
          <div className="grid flex-1 place-items-center p-6 text-center">
            <div><h2 className="font-medium">选择或新建 Chat Session</h2><p className="mt-2 text-sm text-muted-foreground">知识问答不会创建 Evidence、Approval 或 Connector Command。</p></div>
          </div>
        )}
        {error ? <p className="border-t p-3 text-sm text-destructive" role="alert">{error}</p> : null}
      </section>
    </main>
  )
}

export function ChatPage() {
  const {sessionId} = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [pendingContent, setPendingContent] = useState<string | null>(null)
  const [connection, setConnection] = useState<"connecting" | "connected" | "reconnecting">("connecting")
  const sessions = useQuery({queryKey: ["chat-sessions"], queryFn: listChatSessions})
  const session = useQuery({
    queryKey: ["chat-session", sessionId],
    queryFn: () => getChatSession(sessionId ?? ""),
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
    mutationFn: (content: string) => sendChatMessage(sessionId ?? "", content),
    onMutate: (content) => setPendingContent(content),
    onSuccess: refresh,
    onSettled: () => { setPendingContent(null); queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}) },
  })
  const retry = useMutation({
    mutationFn: (messageId: string) => retryChatMessage(sessionId ?? "", messageId),
    onSuccess: refresh,
    onSettled: () => queryClient.invalidateQueries({queryKey: ["chat-session", sessionId]}),
  })

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

  const failure = create.error ?? send.error ?? retry.error ?? session.error ?? sessions.error
  const error = failure instanceof ApiError
    ? failure.status === 404 ? "Chat Session 不存在或无权访问。" : failure.message
    : failure ? "Chat 暂时不可用。" : null
  return (
    <ChatView
      sessions={sessions.data ?? []}
      session={session.data ?? null}
      pendingContent={pendingContent}
      connection={connection}
      busy={create.isPending || send.isPending || retry.isPending}
      error={error}
      onCreate={() => create.mutate()}
      onSelect={(id) => navigate(`/chat/${id}`)}
      onSend={(content) => send.mutate(content)}
      onRetry={(messageId) => retry.mutate(messageId)}
    />
  )
}
