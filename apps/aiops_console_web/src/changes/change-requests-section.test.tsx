import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import type { ChangeRequest, KubernetesPhaseExecution } from "@/api/client"
import { ChangeRequestsSection } from "@/changes/change-requests-section"

const draft = {
  target: {api_version: "apps/v1", kind: "Deployment", namespace: "payments", name: "checkout-api"},
  operation: "patch" as const,
  payload: [{op: "replace" as const, path: "/spec/replicas", value: 5}],
  post_checks: [{type: "json_pointer" as const, path: "/spec/replicas", operator: "eq" as const, value: 5}],
}

const revision: NonNullable<ChangeRequest["active_revision"]> = {
  id: "revision-1",
  number: 1,
  status: "validating",
  question: null,
  plan: {summary: "扩容 checkout-api", changes: [draft]},
  validation: {
    status: "succeeded",
    changes: [{
      ordinal: 1,
      status: "succeeded",
      command_id: "command-1",
      policy_error: null,
      secure_inputs: [],
      result: {
        discovery: {
          api_version: "apps/v1", kind: "Deployment", resource: "deployments", namespaced: true,
          verbs: ["get", "patch"],
        },
        live: {exists: true, uid: "uid-1", resource_version: "41"},
        canonical_change: {
          ...draft,
          target: {...draft.target, uid: "uid-1", resource_version: "41"},
          payload: [
            {op: "test", path: "/metadata/uid", value: "uid-1"},
            {op: "replace", path: "/spec/replicas", value: 5},
          ],
        },
        dry_run: {
          diff: [{op: "replace", path: "/spec/replicas", before: 3, after: 5}],
          hash: "a".repeat(64),
        },
      },
    }],
  },
  created_at: 1,
  superseded_at: null,
}

const changeRequest: ChangeRequest = {
  id: "change-1",
  incident_id: "incident-1",
  submitted_by: "operator",
  desired_outcome: "扩容 checkout-api",
  context: "请求量上升",
  status: "awaiting_approval",
  active_phase: {id: "phase-1", sequence: 1, status: "awaiting_approval", created_at: 1, updated_at: 1},
  active_revision: revision,
  revisions: [revision],
  events: [],
  created_at: 1,
  updated_at: 1,
  phase_review: {
    change_request_id: "change-1",
    phase_id: "phase-1",
    revision_id: "revision-1",
    revision_number: 1,
    status: "awaiting_approval",
    environment: "prod",
    summary: "扩容 checkout-api",
    changes: [{
      ordinal: 1,
      target: revision.validation!.changes[0].result!.canonical_change.target,
      target_confirmation: "apps/v1:Deployment:payments/checkout-api",
      operation: "patch",
      canonical_change: revision.validation!.changes[0].result!.canonical_change,
      inverse_change: null,
      rollback: {status: "available"},
      secure_inputs: [],
      diff: revision.validation!.changes[0].result!.dry_run.diff,
      dry_run_hash: "a".repeat(64),
      risk: "medium",
      post_checks: draft.post_checks,
      authority_id: "authority-1",
    }],
    dry_run_expires_at: 600,
    approval: null,
    reviewed_at: 2,
  },
}

describe("ChangeRequestsSection", () => {
  it("renders only the Gateway-owned precondition and server dry-run diff projection", () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[changeRequest]} canManage={false} />
      </QueryClientProvider>,
    )

    expect(markup).toContain("验证通过")
    expect(markup).toContain("uid-1")
    expect(markup).toContain("ResourceVersion")
    expect(markup).toContain("/spec/replicas")
    expect(markup).toContain("Diff hash")
    expect(markup).not.toContain("desired_state")
    expect(markup).not.toContain("kubectl")
  })

  it("renders exact Phase Approval controls from the Authority-scoped review", () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[changeRequest]} canManage />
      </QueryClientProvider>,
    )

    expect(markup).toContain("apps/v1:Deployment:payments/checkout-api")
    expect(markup).toContain("精确目标确认")
    expect(markup).toContain("审批原因")
    expect(markup).toContain("重新认证")
    expect(markup).toContain("回滚已完成步骤")
    expect(markup).toContain("json_pointer")
    expect(markup).toContain("&quot;operator&quot;: &quot;eq&quot;")
    expect(markup).toContain("Secure Input")
    expect(markup).toContain("Source")
  })

  it("shows key hashes and concrete irreversible loss while locking rollback", () => {
    const irreversible: ChangeRequest = {
      ...changeRequest,
      phase_review: {
        ...changeRequest.phase_review!,
        changes: [{
          ...changeRequest.phase_review!.changes[0],
          rollback: {status: "unavailable", concrete_loss: "The current object UID will be permanently lost."},
          secure_inputs: [{
            key_name: "api.token", sha256: "b".repeat(64),
          }],
        }],
      },
    }
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[irreversible]} canManage />
      </QueryClientProvider>,
    )

    expect(markup).toContain("Rollback unavailable")
    expect(markup).toContain("permanently lost")
    expect(markup).toContain("api.token")
    expect(markup).toContain("b".repeat(64))
    expect(markup).toContain("仅停止后续步骤")
  })

  it("renders the approved single-Change execution controls", () => {
    const approved = {
      ...changeRequest,
      status: "approved" as const,
      active_phase: {...changeRequest.active_phase, status: "approved" as const},
      phase_review: {
        ...changeRequest.phase_review!,
        status: "approved" as const,
        approval: {
          id: "approval-1", phase_id: "phase-1", revision_id: "revision-1",
          approver_id: "operator", authority_ids: ["authority-1"], reason: "restore capacity",
          request_id: "req-approval", rollback_policy: "stop_only" as const,
          target_confirmations: ["apps/v1:Deployment:payments/checkout-api"],
          frozen_changes: changeRequest.phase_review!.changes,
          dry_run_expires_at: 600, approved_at: 3, start_expires_at: 900, idempotent: false,
        },
      },
    }
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[approved]} canManage />
      </QueryClientProvider>,
    )

    expect(markup).toContain("Kubernetes Change Execution")
    expect(markup).toContain("执行原因")
    expect(markup).toContain("Timeout (seconds)")
    expect(markup).toContain("执行 Change")
  })

  it("renders ordered forward and rollback steps from the Gateway projection", () => {
    const client = new QueryClient()
    const change = revision.validation!.changes[0].result!.canonical_change
    const steps: KubernetesPhaseExecution["steps"] = [
      {
        id: "execution-1:forward:1", ordinal: 1, direction: "forward", source_ordinal: null,
        command_id: "command-forward-1", status: "rolled_back", change,
        started_at: 4, completed_at: 5, result: null,
        grant: {id: "grant-forward-1", issued_at: 3, expires_at: 63, consumed_at: 3.5, revoked_at: null},
      },
      {
        id: "execution-1:forward:2", ordinal: 2, direction: "forward", source_ordinal: null,
        command_id: "command-forward-2", status: "failed",
        change: {...change, target: {...change.target, name: "checkout-worker"}},
        started_at: 6, completed_at: 7, result: {error_code: "kubernetes_api_rejected"},
        grant: {id: "grant-forward-2", issued_at: 5, expires_at: 65, consumed_at: 5.5, revoked_at: null},
      },
      {
        id: "execution-1:rollback:1", ordinal: 1, direction: "rollback", source_ordinal: 1,
        command_id: "command-rollback-1", status: "started", change,
        started_at: 8, completed_at: null, result: null,
        grant: {id: "grant-rollback-1", issued_at: 7.5, expires_at: 67.5, consumed_at: 8, revoked_at: null},
      },
    ]
    const execution: KubernetesPhaseExecution = {
      id: "execution-1", change_request_id: "change-1", phase_id: "phase-1",
      approval_id: "approval-1", command_id: "command-rollback-1", status: "rolling_back",
      rollback_policy: "rollback_completed", execution_timeout_seconds: 300,
      started_at: 4, completed_at: null, result: {error_code: "kubernetes_api_rejected"},
      grant: steps[2].grant, current_step: steps[2], steps, idempotent: false,
    }
    client.setQueryData(["phase-execution", "change-1"], execution)
    const rollingBack: ChangeRequest = {
      ...changeRequest, status: "rolling_back",
      active_phase: {...changeRequest.active_phase, status: "rolling_back"},
      phase_review: undefined,
    }

    const markup = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[rollingBack]} canManage />
      </QueryClientProvider>,
    )

    expect(markup.indexOf("checkout-api")).toBeLessThan(markup.indexOf("checkout-worker"))
    expect(markup).toContain("回滚中")
    expect(markup).toContain("grant-rollback-1")
  })

  it.each([
    ["succeeded", null, "执行成功", "none"],
    ["failed", "kubernetes_api_rejected", "执行失败", "kubernetes_api_rejected"],
    ["stale", "stale_change", "目标已漂移", "stale_change"],
    ["post_check_failed", "post_check_failed", "Post-check 失败", "post_check_failed"],
    ["unknown_outcome", "execution_outcome_unknown", "结果未知", "execution_outcome_unknown"],
  ] as const)("renders the trustworthy %s execution outcome", (status, errorCode, label, errorLabel) => {
    const client = new QueryClient()
    const grant = {
      id: "grant-1", issued_at: 3, expires_at: 63, consumed_at: 3.5, revoked_at: null,
    }
    const step: KubernetesPhaseExecution["steps"][number] = {
      id: "execution-1:forward:1", ordinal: 1, direction: "forward", source_ordinal: null,
      command_id: "command-1", status,
      change: {
        target: {
          api_version: "apps/v1", kind: "Deployment", namespace: "payments",
          name: "checkout-api", uid: "uid-1", resource_version: "41",
        },
        operation: "patch", payload: [], post_checks: [],
      },
      started_at: 4, completed_at: 5,
      result: errorCode ? {error_code: errorCode} : null, grant,
    }
    const execution: KubernetesPhaseExecution = {
      id: "execution-1", change_request_id: "change-1", phase_id: "phase-1",
      approval_id: "approval-1", command_id: "command-1", status,
      rollback_policy: "stop_only",
      execution_timeout_seconds: 300, started_at: 4, completed_at: 5,
      result: errorCode ? {error_code: errorCode} : null,
      grant, current_step: step, steps: [step],
      idempotent: false,
    }
    client.setQueryData(["phase-execution", "change-1"], execution)
    const projectedStatus: ChangeRequest["status"] = status === "succeeded" ? "succeeded" : status === "unknown_outcome" ? "unknown_outcome" : "failed"
    const terminal: ChangeRequest = {
      ...changeRequest,
      status: projectedStatus,
      active_phase: {
        ...changeRequest.active_phase,
        status: projectedStatus,
      },
      phase_review: undefined,
    }
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <ChangeRequestsSection incidentId="incident-1" changeRequests={[terminal]} canManage />
      </QueryClientProvider>,
    )

    expect(markup).toContain(label)
    expect(markup).toContain(errorLabel)
    expect(markup).toContain("command-1")
    expect(markup).toContain("grant-1")
  })
})
