import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { GitPullRequestCreateIcon, KeyRoundIcon, RefreshCwIcon, SendIcon, ShieldCheckIcon } from "lucide-react"

import {
  ApiError,
  approveKubernetesPhase,
  createChangeRequest,
  reauthenticate,
  retryChangeRequestPlanning,
  submitChangeRequestInput,
  type ChangeRequest,
  type KubernetesPhaseReview,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { MonoValue } from "@/prototype/shared"

const statusLabel = {
  planning: "规划中",
  needs_input: "需要输入",
  validating: "等待验证",
  awaiting_approval: "等待审批",
  approved: "已审批",
  expired: "已过期",
}

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

function jsonValue(value: unknown) {
  return JSON.stringify(value, null, 2) ?? "null"
}

function mutationError(error: Error | null) {
  if (!(error instanceof ApiError)) return error ? "提交失败" : null
  if (error.code === "secure_input_required") return "敏感值必须通过 Secure Input 提交。"
  if (error.code === "executable_proposal_forbidden") return "请描述期望结果，不要提交可执行配置或命令。"
  return error.message
}

function PhaseApprovalPanel({
  incidentId,
  changeRequestId,
  review,
  canManage,
}: {
  incidentId: string
  changeRequestId: string
  review: KubernetesPhaseReview
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const [confirmation, setConfirmation] = useState("")
  const [reason, setReason] = useState("")
  const [password, setPassword] = useState("")
  const [rollbackPolicy, setRollbackPolicy] = useState<"stop_only" | "rollback_completed">("rollback_completed")
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const expected = review.changes.map((change) => change.target_confirmation)
  const supplied = confirmation.split("\n").map((value) => value.trim()).filter(Boolean)
  const exact = supplied.length === expected.length && supplied.every((value, index) => value === expected[index])
  const refresh = () => queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]})
  const reauth = useMutation({
    mutationFn: reauthenticate,
    onSuccess: () => setPassword(""),
  })
  const approve = useMutation({
    mutationFn: () => approveKubernetesPhase(changeRequestId, {
      revision_id: review.revision_id,
      dry_run_hashes: review.changes.map((change) => change.dry_run_hash),
      target_confirmations: supplied,
      rollback_policy: rollbackPolicy,
      reason,
      idempotency_key: idempotencyKey,
    }),
    onSettled: refresh,
  })

  return <div className="mt-3 border-t pt-3 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <ShieldCheckIcon className="size-4 text-muted-foreground" />
      <span className="font-medium">Exact Change Plan Phase</span>
      <Badge variant={review.status === "expired" ? "destructive" : review.status === "approved" ? "positive" : "outline"}>
        {statusLabel[review.status]}
      </Badge>
      <Badge variant="outline">{review.environment}</Badge>
    </div>
    <div className="mt-3 grid gap-2">
      {review.changes.map((change) => <div key={change.ordinal} className="grid min-w-0 gap-1 border-l-2 pl-3 text-xs">
        <MonoValue>{change.target_confirmation}</MonoValue>
        <div className="flex flex-wrap gap-2 text-muted-foreground">
          <span>Risk · {change.risk}</span>
          <span>Diff hash · <MonoValue>{change.dry_run_hash}</MonoValue></span>
        </div>
        <div className="mt-1 grid gap-1">
          <span className="text-muted-foreground">Post-check</span>
          {change.post_checks.map((check, index) => <pre
            key={`${change.ordinal}:post-check:${index}`}
            className="min-w-0 overflow-x-auto whitespace-pre-wrap break-all border-l pl-2 font-mono"
          >{jsonValue(check)}</pre>)}
        </div>
      </div>)}
      <div className="text-xs text-muted-foreground">
        Dry-run expires · <time dateTime={new Date(review.dry_run_expires_at * 1000).toISOString()}>{new Date(review.dry_run_expires_at * 1000).toLocaleString("zh-CN")}</time>
      </div>
    </div>
    {review.approval ? <dl className="mt-3 grid gap-1 border-t pt-3 text-xs sm:grid-cols-2">
      <div><dt className="text-muted-foreground">审批原因</dt><dd>{review.approval.reason}</dd></div>
      <div><dt className="text-muted-foreground">回滚策略</dt><dd>{review.approval.rollback_policy === "rollback_completed" ? "回滚已完成步骤" : "仅停止后续步骤"}</dd></div>
      <div><dt className="text-muted-foreground">Approver</dt><dd><MonoValue>{review.approval.approver_id}</MonoValue></dd></div>
      <div><dt className="text-muted-foreground">Start expires</dt><dd><time dateTime={new Date(review.approval.start_expires_at * 1000).toISOString()}>{new Date(review.approval.start_expires_at * 1000).toLocaleString("zh-CN")}</time></dd></div>
    </dl> : null}
    {review.status === "awaiting_approval" && canManage ? <div className="mt-3 grid gap-3 border-t pt-3">
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={(event) => { event.preventDefault(); reauth.mutate(password) }}>
        <label className="grid min-w-0 flex-1 gap-1 text-xs font-medium">
          重新认证
          <Input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        </label>
        <Button type="submit" size="sm" variant="outline" disabled={!password || reauth.isPending}><KeyRoundIcon />验证</Button>
      </form>
      <form className="grid gap-3" onSubmit={(event) => { event.preventDefault(); approve.mutate() }}>
        <label className="grid gap-1 text-xs font-medium">
          精确目标确认
          <Textarea
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            placeholder={expected.join("\n")}
            rows={Math.max(2, expected.length)}
            required
          />
        </label>
        <label className="grid gap-1 text-xs font-medium">
          审批原因
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required />
        </label>
        <label className="grid gap-1 text-xs font-medium">
          回滚策略
          <Select value={rollbackPolicy} onValueChange={(value) => setRollbackPolicy(value as typeof rollbackPolicy)}>
            <SelectTrigger><SelectValue>{rollbackPolicy === "rollback_completed" ? "回滚已完成步骤" : "仅停止后续步骤"}</SelectValue></SelectTrigger>
            <SelectContent>
              <SelectItem value="rollback_completed">回滚已完成步骤</SelectItem>
              <SelectItem value="stop_only">仅停止后续步骤</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {reauth.isSuccess ? <span className="text-xs text-muted-foreground">认证已刷新</span> : null}
          {mutationError(reauth.error) || mutationError(approve.error) ? <span role="alert" className="text-xs text-destructive">{mutationError(reauth.error) || mutationError(approve.error)}</span> : null}
          <Button type="submit" disabled={!exact || !reason.trim() || approve.isPending}><ShieldCheckIcon />审批 Phase</Button>
        </div>
      </form>
    </div> : null}
  </div>
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
          {changeRequest.phase_review ? <PhaseApprovalPanel
            incidentId={incidentId}
            changeRequestId={changeRequest.id}
            review={changeRequest.phase_review}
            canManage={canManage}
          /> : null}
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
