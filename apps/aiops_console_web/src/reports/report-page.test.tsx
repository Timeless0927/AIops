import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import { ChangeGovernanceHistory } from "@/reports/report-page"

describe("ChangeGovernanceHistory", () => {
  it("renders approval, execution, rollback, reconciliation, and transition details", () => {
    const markup = renderToStaticMarkup(<ChangeGovernanceHistory changeRequests={[{
      id: "change-1",
      desired_outcome: "Restore checkout availability",
      submitted_by: "user-1",
      created_at: 1_000,
      phases: [{
        id: "phase-1",
        sequence: 1,
        status: "effect_observed",
        revisions: [{
          id: "revision-1", number: 1, status: "validating",
          plan: {
            summary: "Restart checkout through an annotation rollout",
            changes: [{
              operation: "patch",
              target: {kind: "Deployment", namespace: "payments", name: "checkout-api"},
            }],
          },
        }],
        approval: {
          approver_id: "approver-1",
          reason: "reviewed exact diff",
          rollback_policy: "rollback_completed",
          approved_at: 1_001,
        },
        execution: {
          id: "execution-1",
          actor_id: "approver-1",
          reason: "restore service",
          status: "unknown_outcome",
          completed_at: 1_302,
          steps: [
            {
              id: "step-forward", direction: "forward", command_id: "command-1",
              status: "succeeded", change: {
                operation: "patch",
                target: {kind: "Deployment", namespace: "payments", name: "checkout-api"},
              },
            },
            {
              id: "step-rollback", direction: "rollback", command_id: "command-2",
              status: "failed", change: {
                operation: "patch",
                target: {kind: "Deployment", namespace: "payments", name: "checkout-api"},
              },
              result: {error_code: "post_check_failed", error_message: "readiness failed"},
            },
          ],
          reconciliations: [{
            id: "reconciliation-1", classification: "effect_observed", state: "accepted",
            evidence_sha256: "a".repeat(64), accepted_by: "approver-1",
            acceptance_reason: "effect confirmed",
          }],
        },
      }],
      events: [{id: 1, type: "change_request.reconciliation_observed", created_at: 1_303}],
    }]} />)

    expect(markup).toContain("Phase Approval")
    expect(markup).toContain("Plan revisions")
    expect(markup).toContain("Revision 1")
    expect(markup).toContain("Restart checkout through an annotation rollout")
    expect(markup).toContain("reviewed exact diff")
    expect(markup).toContain("rollback")
    expect(markup).toContain("post_check_failed")
    expect(markup).toContain("effect_observed")
    expect(markup).toContain("effect confirmed")
    expect(markup).toContain("change_request.reconciliation_observed")
  })
})
