import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router"
import { describe, expect, it } from "vitest"

import type { ChangeCenterDetail, ChangeCenterSummary } from "@/changes/change-client"
import {
  ChangeCenterDetailView,
  ChangeCenterListView,
  changeCenterFilters,
  filterChangeCenter,
} from "@/changes/change-center-page"

const summaries: ChangeCenterSummary[] = [
  {
    id: "change-input",
    incident: {
      id: "incident-1", title: "Checkout latency", severity: "critical",
      lifecycle_state: "firing",
    },
    desired_outcome: "Clarify the stable revision",
    status: "needs_input", environment: "prod", attention: "input", updated_at: 2,
  },
  {
    id: "change-done",
    incident: {
      id: "incident-2", title: "Worker backlog", severity: "high",
      lifecycle_state: "resolved",
    },
    desired_outcome: "Restore worker capacity",
    status: "succeeded", environment: "staging", attention: null, updated_at: 1,
  },
]

describe("ChangeCenterPage", () => {
  it("owns bounded status and Environment filters in URL state", () => {
    expect(changeCenterFilters(new URLSearchParams("status=attention&environment=prod"))).toEqual({
      status: "attention", environment: "prod",
    })
    expect(changeCenterFilters(new URLSearchParams("status=secret&environment=other"))).toEqual({
      status: "all", environment: "all",
    })
    expect(filterChangeCenter(summaries, {status: "terminal", environment: "staging"})).toEqual([
      summaries[1],
    ])
  })

  it("renders only matching work with source Incident links and pending count", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <ChangeCenterListView
          changeRequests={summaries}
          pendingCount={1}
          filters={{status: "attention", environment: "prod"}}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("待处理 1")
    expect(markup).toContain("Checkout latency")
    expect(markup).toContain("Clarify the stable revision")
    expect(markup).toContain("/changes/change-input?status=attention&amp;environment=prod")
    expect(markup).not.toContain("Worker backlog")
    expect(markup).toContain("min-w-0")
  })

  it("reuses actor-scoped detail and governance without the Incident composer", () => {
    const detail: ChangeCenterDetail = {
      incident: summaries[0].incident,
      environment: "prod",
      attention: "input",
      can_manage: true,
      evidence_references: ["prometheus://query/checkout-errors"],
      change_request: {
        id: "change-input", incident_id: "incident-1", submitted_by: "user-1",
        desired_outcome: "Clarify the stable revision", context: "",
        status: "needs_input",
        active_phase: {
          id: "phase-1", sequence: 1, status: "needs_input", created_at: 1, updated_at: 2,
        },
        active_revision: {
          id: "revision-1", number: 1, status: "needs_input",
          question: "Which revision?", plan: null, validation: null,
          created_at: 2, superseded_at: null,
        },
        revisions: [],
        events: [{
          id: 1, type: "change_request.execution_outcome_unknown", actor_id: null,
          payload: {outcome: "unknown_outcome"}, created_at: 2,
        }],
        created_at: 1, updated_at: 2,
      },
    }
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ChangeCenterDetailView detail={detail} />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(markup).toContain("Checkout latency")
    expect(markup).toContain("Which revision?")
    expect(markup).toContain("/incidents/incident-1")
    expect(markup).toContain("prometheus://query/checkout-errors")
    expect(markup).toContain("执行结果未知")
    expect(markup).toContain("Governance event 历史")
    expect(markup).not.toContain("创建变更请求")
    expect(markup).not.toContain("Desired outcome")
  })
})
