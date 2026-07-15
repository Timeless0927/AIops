import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { GitPullRequestCreateIcon, RefreshCwIcon, SendIcon } from "lucide-react"

import {
  createChangeRequest,
  newClientId,
  retryChangeRequestPlanning,
  submitChangeRequestInput,
  type ChangeRequest,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { MonoValue } from "@/prototype/shared"
import {
  ChangeRequestGovernance,
  jsonValue,
  mutationError,
  refreshChangeRequestViews,
  statusLabel,
} from "./change-request-governance"
import { SecureInputForm } from "./secure-input-form"

const validationStatusLabel = {
  pending: "验证中",
  succeeded: "验证通过",
  failed: "验证失败",
}

const operationLabel = {
  create: "Create",
  patch: "Patch",
  delete: "Delete",
}

export function ChangeRequestsSection({
  incidentId,
  changeRequests,
  canManage,
  showComposer = true,
}: {
  incidentId: string
  changeRequests: ChangeRequest[]
  canManage: boolean
  showComposer?: boolean
}) {
  const queryClient = useQueryClient()
  const [desiredOutcome, setDesiredOutcome] = useState("")
  const [changeContext, setChangeContext] = useState("")
  const [clarification, setClarification] = useState("")
  const [clarifyingId, setClarifyingId] = useState("")
  const refreshChangeViews = () => refreshChangeRequestViews(queryClient, incidentId)
  const createChange = useMutation({
    mutationFn: () => createChangeRequest(incidentId, {
      desired_outcome: desiredOutcome,
      context: changeContext,
      idempotency_key: newClientId(),
    }),
    onSuccess: () => {
      setDesiredOutcome("")
      setChangeContext("")
    },
    onSettled: refreshChangeViews,
  })
  const answerChange = useMutation({
    mutationFn: ({id, content}: {id: string; content: string}) => submitChangeRequestInput(id, {
      content,
      idempotency_key: newClientId(),
    }),
    onSuccess: () => {
      setClarification("")
      setClarifyingId("")
    },
    onSettled: refreshChangeViews,
  })
  const retryPlanning = useMutation({
    mutationFn: retryChangeRequestPlanning,
    onSettled: refreshChangeViews,
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
          {revision?.plan ? <div className="mt-3 space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{revision.plan.summary}</span>
              {revision.validation ? <Badge variant={revision.validation.status === "failed" ? "destructive" : "outline"}>
                {validationStatusLabel[revision.validation.status]}
              </Badge> : null}
            </div>
            {revision.plan.changes.map((change, index) => {
              const validation = revision.validation?.changes.find((item) => item.ordinal === index + 1)
              const result = validation?.result
              return <div key={`${revision.id}:${index}`} className="grid min-w-0 gap-2 border-l-2 pl-3 text-xs">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <Badge variant="outline">{operationLabel[change.operation]}</Badge>
                  <MonoValue>{change.target.api_version} · {change.target.kind} · {change.target.namespace ?? "cluster"}/{change.target.name}</MonoValue>
                </div>
                <div className="text-muted-foreground">验证：{change.post_checks.map((check) => check.type).join(" · ")}</div>
                {validation?.policy_error ? <div role="alert" className="break-words text-destructive">
                  <MonoValue>{validation.policy_error.code}</MonoValue> · {validation.policy_error.message}
                </div> : null}
                {result ? <div className="min-w-0 border-t pt-2">
                  <dl className="grid gap-1 sm:grid-cols-2">
                    <div><dt className="text-muted-foreground">UID</dt><dd><MonoValue>{result.live.uid ?? "new object"}</MonoValue></dd></div>
                    <div><dt className="text-muted-foreground">ResourceVersion</dt><dd><MonoValue>{result.live.resource_version ?? "new object"}</MonoValue></dd></div>
                  </dl>
                  <div className="mt-2 grid gap-2">
                    {result.dry_run.diff.length ? result.dry_run.diff.map((entry, diffIndex) => <div key={`${entry.path}:${diffIndex}`} className="grid min-w-0 gap-1 border-t pt-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_minmax(0,1fr)]">
                      <div className="min-w-0"><Badge variant="outline">{entry.op}</Badge><MonoValue>{entry.path || "/"}</MonoValue></div>
                      <pre className="min-w-0 whitespace-pre-wrap break-all text-muted-foreground">{jsonValue(entry.before)}</pre>
                      <pre className="min-w-0 whitespace-pre-wrap break-all">{jsonValue(entry.after)}</pre>
                    </div>) : <div className="text-muted-foreground">API Server 未产生对象差异</div>}
                  </div>
                  <div className="mt-2 truncate text-muted-foreground" title={result.dry_run.hash}>Diff hash · <MonoValue>{result.dry_run.hash}</MonoValue></div>
                </div> : null}
              </div>
            })}
          </div> : null}
          <ChangeRequestGovernance
            incidentId={incidentId}
            changeRequest={changeRequest}
            canManage={canManage}
          />
        </article>
      })}
      {canManage && showComposer ? <SecureInputForm onCreated={(placeholder) => {
        setChangeContext((current) => [current.trim(), placeholder].filter(Boolean).join("\n"))
      }} /> : null}
      {canManage && showComposer ? <form className="grid gap-3 border-t pt-4" onSubmit={(event) => { event.preventDefault(); createChange.mutate() }}>
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
