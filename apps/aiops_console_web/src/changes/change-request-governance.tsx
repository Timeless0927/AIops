import { useState } from "react"
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query"
import { ActivityIcon, BanIcon, CheckCircleIcon, KeyRoundIcon, PlayIcon, ShieldCheckIcon } from "lucide-react"

import {
  ApiError,
  acceptKubernetesReconciliation,
  approveKubernetesPhase,
  cancelKubernetesPhaseExecution,
  getKubernetesPhaseExecution,
  newClientId,
  reauthenticate,
  startKubernetesPhaseExecution,
  type ChangeRequest,
  type KubernetesPhaseReview,
} from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { MonoValue } from "@/prototype/shared"

export const statusLabel = {
  planning: "规划中",
  needs_input: "需要输入",
  validating: "等待验证",
  awaiting_approval: "等待审批",
  approved: "已审批",
  expired: "已过期",
  executing: "执行中",
  succeeded: "已成功",
  failed: "已失败",
  unknown_outcome: "结果未知",
  effect_observed: "已观察到效果",
  cancel_requested: "取消中",
  cancelled: "已取消",
  rolling_back: "回滚中",
  rolled_back: "已回滚",
  rollback_failed: "回滚失败",
  secure_input_unavailable: "Secure Input 不可用",
}

const executionStatusLabel = {
  pending: "等待前序步骤",
  queued: "等待 Connector",
  dispatched: "已下发",
  started: "执行中",
  succeeded: "执行成功",
  failed: "执行失败",
  stale: "目标已漂移",
  post_check_failed: "Post-check 失败",
  unknown_outcome: "结果未知",
  effect_observed: "已观察到效果",
  cancel_requested: "等待当前步骤结束",
  cancelled: "已取消",
  rolling_back: "回滚中",
  rolled_back: "回滚完成",
  rollback_failed: "回滚失败",
  secure_input_unavailable: "Secure Input 不可用",
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

export function jsonValue(value: unknown) {
  return JSON.stringify(value, null, 2) ?? "null"
}

export function mutationError(error: Error | null) {
  if (!(error instanceof ApiError)) return error ? "提交失败" : null
  if (error.code === "secure_input_required") return "敏感值必须通过 Secure Input 提交。"
  if (error.code === "executable_proposal_forbidden") return "请描述期望结果，不要提交可执行配置或命令。"
  return error.message
}

export function refreshChangeRequestViews(queryClient: QueryClient, incidentId: string) {
  return Promise.all([
    queryClient.invalidateQueries({queryKey: ["incidents", incidentId, "workbench"]}),
    queryClient.invalidateQueries({queryKey: ["change-center"]}),
  ])
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
  const [idempotencyKey] = useState(newClientId)
  const expected = review.changes.map((change) => change.target_confirmation)
  const displayedChanges = review.approval?.frozen_changes ?? review.changes
  const supplied = confirmation.split("\n").map((value) => value.trim()).filter(Boolean)
  const exact = supplied.length === expected.length && supplied.every((value, index) => value === expected[index])
  const irreversible = review.changes.some((change) => change.rollback.status === "unavailable")
  const selectedRollbackPolicy = irreversible ? "stop_only" : rollbackPolicy
  const refresh = () => refreshChangeRequestViews(queryClient, incidentId)
  const reauth = useMutation({
    mutationFn: reauthenticate,
    onSuccess: () => setPassword(""),
  })
  const approve = useMutation({
    mutationFn: () => approveKubernetesPhase(changeRequestId, {
      revision_id: review.revision_id,
      dry_run_hashes: review.changes.map((change) => change.dry_run_hash),
      target_confirmations: supplied,
      rollback_policy: selectedRollbackPolicy,
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
      {displayedChanges.map((change) => <div key={change.ordinal} className="grid min-w-0 gap-1 border-l-2 pl-3 text-xs">
        <MonoValue>{change.target_confirmation}</MonoValue>
        <div className="flex flex-wrap gap-2 text-muted-foreground">
          <span>Risk · {change.risk}</span>
          <span>Diff hash · <MonoValue>{change.dry_run_hash}</MonoValue></span>
        </div>
        <div className="mt-1 grid gap-1">
          <span className="text-muted-foreground">
            {review.approval ? "Frozen approval diff" : "API Server dry-run diff"}
          </span>
          {change.diff.length ? change.diff.map((entry, index) => <div
            key={`${entry.path}:${index}`}
            className="grid min-w-0 gap-1 border-l pl-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_minmax(0,1fr)]"
          >
            <div className="min-w-0 break-all"><Badge variant="outline">{entry.op}</Badge> <MonoValue>{entry.path || "/"}</MonoValue></div>
            <pre className="min-w-0 whitespace-pre-wrap break-all text-muted-foreground">{jsonValue(entry.before)}</pre>
            <pre className="min-w-0 whitespace-pre-wrap break-all">{jsonValue(entry.after)}</pre>
          </div>) : <span className="text-muted-foreground">API Server 未产生对象差异</span>}
        </div>
        <div className="mt-1 grid gap-1">
          <span className="text-muted-foreground">Post-check</span>
          {change.post_checks.map((check, index) => <pre
            key={`${change.ordinal}:post-check:${index}`}
            className="min-w-0 overflow-x-auto whitespace-pre-wrap break-all border-l pl-2 font-mono"
          >{jsonValue(check)}</pre>)}
        </div>
        {change.secure_inputs.length ? <dl className="mt-1 grid gap-1 text-muted-foreground">
          {change.secure_inputs.map((input) => <div key={`${input.key_name}:${input.sha256}`} className="flex min-w-0 flex-wrap gap-2">
            <dt>{input.key_name}</dt><dd><MonoValue>{input.sha256}</MonoValue></dd>
          </div>)}
        </dl> : null}
        {change.rollback.status === "unavailable" ? <div className="mt-1 border-l-2 border-destructive pl-2 text-destructive">
          <div className="font-medium">Rollback unavailable</div>
          <div>{change.rollback.concrete_loss}</div>
        </div> : null}
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
          <Select value={selectedRollbackPolicy} disabled={irreversible} onValueChange={(value) => setRollbackPolicy(value as typeof rollbackPolicy)}>
            <SelectTrigger><SelectValue>{selectedRollbackPolicy === "rollback_completed" ? "回滚已完成步骤" : "仅停止后续步骤"}</SelectValue></SelectTrigger>
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

function PhaseExecutionPanel({
  incidentId,
  changeRequestId,
  phaseId,
  canStart,
  canCancel,
}: {
  incidentId: string
  changeRequestId: string
  phaseId: string
  canStart: boolean
  canCancel: boolean
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState("")
  const [cancelReason, setCancelReason] = useState("")
  const [reconciliationReason, setReconciliationReason] = useState("")
  const [reconciliationPassword, setReconciliationPassword] = useState("")
  const [timeout, setTimeout] = useState(300)
  const [idempotencyKey] = useState(newClientId)
  const [cancelIdempotencyKey] = useState(newClientId)
  const [reconciliationIdempotencyKey] = useState(newClientId)
  const execution = useQuery({
    queryKey: ["phase-execution", changeRequestId],
    queryFn: () => getKubernetesPhaseExecution(changeRequestId),
    refetchInterval: (query) => query.state.data && ["queued", "dispatched", "started", "cancel_requested", "rolling_back", "unknown_outcome", "effect_observed"].includes(query.state.data.status) ? 2000 : false,
  })
  const start = useMutation({
    mutationFn: () => startKubernetesPhaseExecution(changeRequestId, {
      phase_id: phaseId,
      reason,
      idempotency_key: idempotencyKey,
      execution_timeout_seconds: timeout,
    }),
    onSettled: () => {
      queryClient.invalidateQueries({queryKey: ["phase-execution", changeRequestId]})
      refreshChangeRequestViews(queryClient, incidentId)
    },
  })
  const cancel = useMutation({
    mutationFn: () => cancelKubernetesPhaseExecution(changeRequestId, {
      phase_id: phaseId,
      reason: cancelReason,
      idempotency_key: cancelIdempotencyKey,
    }),
    onSettled: () => {
      queryClient.invalidateQueries({queryKey: ["phase-execution", changeRequestId]})
      refreshChangeRequestViews(queryClient, incidentId)
    },
  })
  const current = execution.data ?? start.data
  const acceptReconciliation = useMutation({
    mutationFn: () => acceptKubernetesReconciliation(changeRequestId, {
      phase_id: phaseId,
      evidence_sha256: current!.reconciliation!.evidence_sha256,
      reason: reconciliationReason,
      idempotency_key: reconciliationIdempotencyKey,
    }),
    onSettled: () => {
      queryClient.invalidateQueries({queryKey: ["phase-execution", changeRequestId]})
      refreshChangeRequestViews(queryClient, incidentId)
    },
  })
  const reconciliationReauth = useMutation({
    mutationFn: reauthenticate,
    onSuccess: () => setReconciliationPassword(""),
  })
  const result = current?.result
  const errorCode = typeof result?.error_code === "string" ? result.error_code : "none"

  return <div className="mt-3 border-t pt-3 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <ActivityIcon className="size-4 text-muted-foreground" />
      <span className="font-medium">Kubernetes Change Execution</span>
      {current ? <Badge variant={["succeeded", "rolled_back"].includes(current.status) ? "positive" : ["failed", "stale", "post_check_failed", "rollback_failed", "secure_input_unavailable"].includes(current.status) ? "destructive" : "outline"}>
        {executionStatusLabel[current.status]}
      </Badge> : <Badge variant="outline">未开始</Badge>}
    </div>
    {mutationError(cancel.error) ? <div role="alert" className="mt-2 text-xs text-destructive">{mutationError(cancel.error)}</div> : null}
    {mutationError(acceptReconciliation.error) ? <div role="alert" className="mt-2 text-xs text-destructive">{mutationError(acceptReconciliation.error)}</div> : null}
    {current ? <dl className="mt-3 grid gap-1 text-xs sm:grid-cols-2">
      <div><dt className="text-muted-foreground">Command</dt><dd><MonoValue>{current.command_id}</MonoValue></dd></div>
      <div><dt className="text-muted-foreground">Execution Grant</dt><dd><MonoValue>{current.grant?.id ?? "none"}</MonoValue></dd></div>
      <div><dt className="text-muted-foreground">Timeout</dt><dd>{current.execution_timeout_seconds}s</dd></div>
      <div><dt className="text-muted-foreground">Error</dt><dd><MonoValue>{errorCode}</MonoValue></dd></div>
    </dl> : null}
    {current ? <ol className="mt-3 divide-y border-y text-xs" aria-label="Execution steps">
      {current.steps.map((step) => <li key={step.id} className="grid gap-1 py-2 sm:grid-cols-[5rem_7rem_1fr_auto] sm:items-center">
        <span className="text-muted-foreground">#{step.ordinal} {step.direction === "rollback" ? "回滚" : "执行"}</span>
        <Badge variant={step.status === "succeeded" || step.status === "rolled_back" ? "positive" : step.status === "failed" || step.status === "stale" || step.status === "post_check_failed" ? "destructive" : "outline"}>
          {executionStatusLabel[step.status]}
        </Badge>
        <span className="min-w-0 break-words">{step.change.target.kind} / <MonoValue>{step.change.target.name}</MonoValue></span>
        <MonoValue>{step.grant?.id ?? "no grant"}</MonoValue>
      </li>)}
    </ol> : null}
    {current?.reconciliation ? <dl className="mt-3 grid gap-1 border-y py-2 text-xs sm:grid-cols-2">
      <div><dt className="text-muted-foreground">Reconciliation</dt><dd>{executionStatusLabel[current.reconciliation.classification]}</dd></div>
      <div><dt className="text-muted-foreground">Evidence</dt><dd><MonoValue>{current.reconciliation.evidence_sha256}</MonoValue></dd></div>
    </dl> : null}
    {current?.reconciliation?.state === "observed" && canCancel ? <div className="mt-3 grid gap-3">
      {reconciliationReauth.isSuccess ? <span className="text-xs text-muted-foreground">认证已刷新</span> : null}
      {mutationError(reconciliationReauth.error) ? <span role="alert" className="text-xs text-destructive">{mutationError(reconciliationReauth.error)}</span> : null}
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={(event) => { event.preventDefault(); reconciliationReauth.mutate(reconciliationPassword) }}>
        <label className="grid min-w-0 flex-1 gap-1 text-xs font-medium">
          重新认证
          <Input type="password" autoComplete="current-password" value={reconciliationPassword} onChange={(event) => setReconciliationPassword(event.target.value)} required />
        </label>
        <Button type="submit" size="sm" variant="outline" disabled={!reconciliationPassword || reconciliationReauth.isPending}><KeyRoundIcon />验证</Button>
      </form>
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={(event) => { event.preventDefault(); acceptReconciliation.mutate() }}>
        <label className="grid min-w-0 flex-1 gap-1 text-xs font-medium">
          接受原因
          <Input value={reconciliationReason} onChange={(event) => setReconciliationReason(event.target.value)} maxLength={500} required />
        </label>
        <Button type="submit" size="sm" disabled={acceptReconciliation.isPending || !reconciliationReason.trim()}>
          <CheckCircleIcon data-icon="inline-start" />接受 Reconciliation
        </Button>
      </form>
    </div> : null}
    {current && canCancel && ["queued", "dispatched", "started"].includes(current.status) ? <form className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={(event) => { event.preventDefault(); cancel.mutate() }}>
      <label className="grid min-w-0 flex-1 gap-1 text-xs font-medium">
        取消原因
        <Input value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} maxLength={500} required />
      </label>
      <Button type="submit" variant="destructive" disabled={!cancelReason.trim() || cancel.isPending}><BanIcon />取消后续执行</Button>
    </form> : null}
    {!current && canStart ? <form className="mt-3 grid gap-3" onSubmit={(event) => { event.preventDefault(); start.mutate() }}>
      <label className="grid gap-1 text-xs font-medium">
        执行原因
        <Textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required />
      </label>
      <label className="grid gap-1 text-xs font-medium sm:max-w-48">
        Timeout (seconds)
        <Input type="number" min={300} max={1800} step={60} value={timeout} onChange={(event) => setTimeout(Number(event.target.value))} required />
      </label>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {mutationError(start.error) || mutationError(execution.error) ? <span role="alert" className="text-xs text-destructive">{mutationError(start.error) || mutationError(execution.error)}</span> : null}
        <Button type="submit" disabled={!reason.trim() || timeout < 300 || timeout > 1800 || start.isPending}><PlayIcon />执行 Change</Button>
      </div>
    </form> : null}
  </div>
}

export function ChangeRequestGovernance({
  incidentId,
  changeRequest,
  canManage,
}: {
  incidentId: string
  changeRequest: ChangeRequest
  canManage: boolean
}) {
  return <>
    {changeRequest.phase_review ? <PhaseApprovalPanel
      incidentId={incidentId}
      changeRequestId={changeRequest.id}
      review={changeRequest.phase_review}
      canManage={canManage}
    /> : null}
    {changeRequest.phase_review?.approval || [
      "executing", "succeeded", "failed", "unknown_outcome", "effect_observed",
      "cancel_requested", "cancelled", "rolling_back", "rolled_back", "rollback_failed",
    ].includes(changeRequest.status) ? <PhaseExecutionPanel
      incidentId={incidentId}
      changeRequestId={changeRequest.id}
      phaseId={changeRequest.active_phase.id}
      canStart={canManage && changeRequest.status === "approved"}
      canCancel={canManage}
    /> : null}
  </>
}
