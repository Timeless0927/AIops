import { useMutation, useQueryClient } from "@tanstack/react-query"
import { GitPullRequestCreateIcon, WrenchIcon } from "lucide-react"

import { createChangeRequest, newClientId, type RecommendedAction } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { MonoValue } from "@/prototype/shared"

export function RecommendationsSection({
  incidentId,
  recommendations,
  canManage,
}: {
  incidentId: string
  recommendations: RecommendedAction[]
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const create = useMutation({
    mutationFn: (recommendation: RecommendedAction) => createChangeRequestForRecommendation(
      incidentId, recommendation, newClientId(),
    ),
    onSuccess: () => queryClient.invalidateQueries({
      queryKey: ["incidents", incidentId, "workbench"],
    }),
  })

  return <section className="border-b" aria-labelledby="recommendations-title">
    <header className="flex items-center gap-3 border-b p-4">
      <WrenchIcon className="size-5 text-muted-foreground" />
      <div>
        <h2 id="recommendations-title" className="text-base font-semibold">Recommendations</h2>
        <p className="mt-1 text-xs text-muted-foreground">{recommendations.length} 条 evidence-grounded guidance</p>
      </div>
    </header>
    {recommendations.length ? <div className="divide-y">
      {recommendations.map((recommendation) => <article
        key={`${recommendation.id}:${recommendation.version}`}
        className="grid min-w-0 gap-3 p-4 lg:grid-cols-[minmax(0,1fr)_auto]"
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-medium">{recommendation.summary}</h3>
            <Badge variant={recommendation.stale ? "outline" : recommendation.gate.status === "complete" ? "secondary" : "warning"}>
              {recommendation.stale ? "已过期" : recommendation.gate.status === "complete" ? "证据完整" : "需要更多证据"}
            </Badge>
          </div>
          <div className="mt-2 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <MonoValue>{recommendation.target.cluster_id}/{recommendation.target.namespace}/{recommendation.target.workload_name ?? "unresolved"}</MonoValue>
            <span>v{recommendation.version}</span>
            <MonoValue>{recommendation.hash.slice(0, 12)}</MonoValue>
          </div>
          {recommendation.safeguards.length ? <p className="mt-2 text-xs text-muted-foreground">
            Safeguards: {recommendation.safeguards.join(" · ")}
          </p> : null}
          {recommendation.gate.reasons.length ? <p className="mt-2 text-xs text-muted-foreground">
            {recommendation.gate.reasons.join(" · ")}
          </p> : null}
        </div>
        {canManage ? <div className="flex items-start lg:justify-end">
          <RecommendationCreateButton
            disabled={recommendation.stale || create.isPending}
            onCreate={() => create.mutate(recommendation)}
          />
        </div> : null}
        {create.isError && create.variables?.id === recommendation.id ? <p className="text-xs text-destructive lg:col-span-2" role="alert">
          创建失败，请刷新后重试。
        </p> : null}
      </article>)}
    </div> : <p className="p-4 text-sm text-muted-foreground">尚无 Recommendation</p>}
  </section>
}

export function RecommendationCreateButton({
  disabled,
  onCreate,
}: {
  disabled: boolean
  onCreate: () => void | Promise<unknown>
}) {
  return <Button size="sm" variant="outline" disabled={disabled} onClick={onCreate}>
    <GitPullRequestCreateIcon />创建 Change Request
  </Button>
}

export function createChangeRequestForRecommendation(
  incidentId: string,
  recommendation: RecommendedAction,
  idempotencyKey: string,
  request: typeof createChangeRequest = createChangeRequest,
) {
  return request(
    incidentId,
    changeRequestFromRecommendation(recommendation, idempotencyKey),
  )
}

export function changeRequestFromRecommendation(
  recommendation: RecommendedAction,
  idempotencyKey: string,
) {
  const evidence = recommendation.evidence_step_ids.length
    ? `Evidence steps: ${recommendation.evidence_step_ids.join(", ")}`
    : "Evidence steps: none"
  const safeguards = recommendation.safeguards.length
    ? `Safeguards: ${recommendation.safeguards.join("; ")}`
    : "Safeguards: none"
  return {
    desired_outcome: recommendation.summary,
    context: `Recommendation ${recommendation.id} v${recommendation.version}.\n${evidence}\n${safeguards}`,
    idempotency_key: idempotencyKey,
  }
}
