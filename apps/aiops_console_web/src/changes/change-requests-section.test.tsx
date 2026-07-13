import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import type { ChangeRequest } from "@/api/client"
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
  status: "validating",
  active_phase: {id: "phase-1", sequence: 1, status: "validating", created_at: 1, updated_at: 1},
  active_revision: revision,
  revisions: [revision],
  events: [],
  created_at: 1,
  updated_at: 1,
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
})
