import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/transport"
import { mutationError } from "@/changes/change-request-governance"
import type { RecommendedAction } from "@/incidents/incident-client"
import {
  changeRequestFromRecommendation,
  createChangeRequestForRecommendation,
  RecommendationCreateButton,
  RecommendationsSection,
} from "@/recommendations/recommendations-section"

const recommendation: RecommendedAction = {
  id: "recommendation-1",
  version: 1,
  summary: "Restart checkout pods through a controlled rollout",
  change_intent: "controlled_restart",
  target: {
    cluster_id: "cluster-prod",
    namespace: "payments",
    workload_kind: "Deployment",
    workload_name: "checkout-api",
    deployment_target_id: "target-1",
    resource_binding_id: "binding-1",
    binding_revision: 4,
  },
  evidence_step_ids: ["evidence-k8s", "evidence-metrics"],
  safeguards: ["Preserve availability", "Verify readiness"],
  gate: {status: "complete", reasons: []},
  hash: "a".repeat(64),
  stale: false,
  expires_at: 2_000,
}

describe("RecommendationsSection", () => {
  it("renders guidance with an explicit Change Request command and no direct execution", () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <RecommendationsSection
          incidentId="incident-1"
          recommendations={[recommendation]}
          canManage
        />
      </QueryClientProvider>,
    )

    expect(markup).toContain("创建变更请求（Change Request）")
    expect(markup).toContain("基于证据的建议")
    expect(markup).toContain("Preserve availability")
    expect(markup).not.toContain("批准并执行")
    expect(markup).not.toContain("Rollback Plan")
  })

  it("translates guidance only into desired outcome and context", () => {
    expect(changeRequestFromRecommendation(recommendation, "idempotency-1")).toEqual({
      desired_outcome: recommendation.summary,
      context: [
        "Recommendation recommendation-1 v1.",
        "Evidence steps: evidence-k8s, evidence-metrics",
        "Safeguards: Preserve availability; Verify readiness",
      ].join("\n"),
      idempotency_key: "idempotency-1",
    })
  })

  it("clicks the command and submits the mapped Change Request", async () => {
    const request = vi.fn().mockResolvedValue({})
    const button = RecommendationCreateButton({
      disabled: false,
      onCreate: () => createChangeRequestForRecommendation(
        "incident-1", recommendation, "idempotency-1", request,
      ),
    })

    await button.props.onClick()

    expect(request).toHaveBeenCalledWith("incident-1", {
      desired_outcome: recommendation.summary,
      context: [
        "Recommendation recommendation-1 v1.",
        "Evidence steps: evidence-k8s, evidence-metrics",
        "Safeguards: Preserve availability; Verify readiness",
      ].join("\n"),
      idempotency_key: "idempotency-1",
    })
  })

  it("explains planner unavailability instead of a generic retry", () => {
    expect(mutationError(new ApiError(503, "planner_unavailable", "Diagnosis planning request failed")))
      .toContain("不要重复创建")
  })
})
