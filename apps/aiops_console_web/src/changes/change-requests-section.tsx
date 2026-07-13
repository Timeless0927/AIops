import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { GitPullRequestCreateIcon, RefreshCwIcon, SendIcon } from "lucide-react"

import {
  ApiError,
  createChangeRequest,
  retryChangeRequestPlanning,
  submitChangeRequestInput,
  type ChangeRequest,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { MonoValue } from "@/prototype/shared"

const statusLabel = {
  planning: "规划中",
  needs_input: "需要输入",
  validating: "等待验证",
}

function mutationError(error: Error | null) {
  if (!(error instanceof ApiError)) return error ? "提交失败" : null
  if (error.code === "secure_input_required") return "敏感值必须通过 Secure Input 提交。"
  if (error.code === "executable_proposal_forbidden") return "请描述期望结果，不要提交可执行配置或命令。"
  return error.message
}

export function ChangeRequestsSection({
  incidentId,
  changeRequests,
  canManage,
}: {
  incidentId: string
  changeRequests: ChangeRequest[]
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const [desiredOutcome, setDesiredOutcome] = useState("")
  const [changeContext, setChangeContext] = useState("")
  const [clarification, setClarification] = useState("")
  const [clarifyingId, setClarifyingId] = useState("")
  const workbenchKey = ["incidents", incidentId, "workbench"]
  const refreshWorkbench = () => queryClient.invalidateQueries({queryKey: workbenchKey})
  const createChange = useMutation({
    mutationFn: () => createChangeRequest(incidentId, {
      desired_outcome: desiredOutcome,
      context: changeContext,
      idempotency_key: crypto.randomUUID(),
    }),
    onSuccess: () => {
      setDesiredOutcome("")
      setChangeContext("")
    },
    onSettled: refreshWorkbench,
  })
  const answerChange = useMutation({
    mutationFn: ({id, content}: {id: string; content: string}) => submitChangeRequestInput(id, {
      content,
      idempotency_key: crypto.randomUUID(),
    }),
    onSuccess: () => {
      setClarification("")
      setClarifyingId("")
    },
    onSettled: refreshWorkbench,
  })
  const retryPlanning = useMutation({
    mutationFn: retryChangeRequestPlanning,
    onSettled: refreshWorkbench,
  })

  return <section className="border-b" aria-labelledby="changes-title">
    <header className="flex items-center gap-3 border-b p-4">
      <GitPullRequestCreateIcon className="size-5 text-muted-foreground" />
      <div>
        <h2 id="changes-title" className="text-base font-semibold">变更请求</h2>
        <p className="mt-1 text-xs text-muted-foreground">{changeRequests.length} 个请求</p>
      </div>
    </header>
    <div className="space-y-3 p-4">
      {changeRequests.map((changeRequest) => {
        const revision = changeRequest.active_revision
        return <article key={changeRequest.id} className="rounded-md border p-3">
          <div className="flex flex-wrap items-start gap-2">
            <div className="min-w-0 flex-1">
              <div className="font-medium break-words">{changeRequest.desired_outcome}</div>
              {changeRequest.context ? <div className="mt-1 text-xs text-muted-foreground break-words">{changeRequest.context}</div> : null}
            </div>
            <Badge variant={changeRequest.status === "needs_input" ? "outline" : "secondary"}>{statusLabel[changeRequest.status]}</Badge>
            {revision ? <Badge variant="outline">v{revision.number}</Badge> : null}
          </div>
          {changeRequest.status === "planning" && canManage ? <div className="mt-3 flex items-center justify-end gap-2">
            {retryPlanning.isError ? <span className="text-xs text-destructive">{mutationError(retryPlanning.error)}</span> : null}
            <Button variant="outline" size="sm" disabled={retryPlanning.isPending} onClick={() => retryPlanning.mutate(changeRequest.id)}>
              <RefreshCwIcon />重试规划
            </Button>
          </div> : null}
          {revision?.question ? <div className="mt-3 border-l-2 pl-3 text-sm">
            <div className="font-medium">{revision.question}</div>
            {canManage ? <form className="mt-2 flex flex-col gap-2 sm:flex-row" onSubmit={(event) => {
              event.preventDefault()
              answerChange.mutate({id: changeRequest.id, content: clarification})
            }}>
              <Textarea
                value={clarifyingId === changeRequest.id ? clarification : ""}
                onFocus={() => {
                  if (clarifyingId !== changeRequest.id) {
                    setClarifyingId(changeRequest.id)
                    setClarification("")
                  }
                }}
                onChange={(event) => { setClarifyingId(changeRequest.id); setClarification(event.target.value) }}
                aria-label="变更请求补充输入"
                maxLength={4000}
                required
              />
              <Button className="sm:self-end" type="submit" disabled={clarifyingId !== changeRequest.id || !clarification.trim() || answerChange.isPending}>
                <SendIcon />提交
              </Button>
            </form> : null}
            {answerChange.isError && clarifyingId === changeRequest.id ? <div className="mt-2 text-xs text-destructive">{mutationError(answerChange.error)}</div> : null}
          </div> : null}
          {revision?.plan ? <div className="mt-3 space-y-2 text-sm">
            <div className="font-medium">{revision.plan.summary}</div>
            {revision.plan.changes.map((change, index) => <div key={`${revision.id}:${index}`} className="grid gap-1 border-l-2 pl-3 text-xs">
              <MonoValue>{change.target.api_version} · {change.target.kind} · {change.target.namespace ?? "cluster"}/{change.target.name}</MonoValue>
              <span>{change.desired_state}</span>
              <span className="text-muted-foreground">Post-check: {change.post_check}</span>
            </div>)}
          </div> : null}
        </article>
      })}
      {canManage ? <form className="grid gap-3 border-t pt-4" onSubmit={(event) => { event.preventDefault(); createChange.mutate() }}>
        <label className="grid gap-1.5 text-sm font-medium">
          Desired outcome
          <Textarea value={desiredOutcome} onChange={(event) => setDesiredOutcome(event.target.value)} maxLength={2000} required />
        </label>
        <label className="grid gap-1.5 text-sm font-medium">
          Context
          <Textarea value={changeContext} onChange={(event) => setChangeContext(event.target.value)} maxLength={4000} />
        </label>
        <div className="flex items-center justify-end gap-3">
          {createChange.isError ? <span className="text-xs text-destructive">{mutationError(createChange.error)}</span> : null}
          <Button type="submit" disabled={!desiredOutcome.trim() || createChange.isPending}><GitPullRequestCreateIcon />创建变更请求</Button>
        </div>
      </form> : null}
    </div>
  </section>
}
