import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router"
import { describe, expect, it } from "vitest"

import { ResourceWorkspaceView, filterResources, resourceFilters } from "@/resources/resource-workspace-page"

const data = {
  request_id: "req-1", can_administer: true,
  clusters: [{id: "cluster-1", name: "Production", environment: "prod" as const, runtime_status: "offline" as const, read_verification: "verified" as const}, {id: "cluster-idle", name: "Idle", environment: "test" as const, runtime_status: "online" as const, read_verification: "unverified" as const}],
  services: [{id: "service-1", team_id: "team-1", team_name: "Payments", name: "Checkout", active: true}, {id: "service-idle", team_id: "team-1", team_name: "Payments", name: "Worker", active: false}],
  resources: [
    {id: "target-1", cluster_id: "cluster-1", namespace: "payments", kind: "Deployment", name: "checkout-api", service_id: "service-1", team_id: "team-1", binding_state: "bound" as const, availability: "unavailable" as const},
    {id: "candidate-2", cluster_id: "cluster-1", namespace: "payments", kind: "Deployment", name: "worker", service_id: null, team_id: null, binding_state: "unbound" as const, availability: "unbound" as const},
  ],
}

describe("ResourceWorkspacePage", () => {
  it("owns bounded URL filters", () => {
    expect(resourceFilters(new URLSearchParams("cluster=cluster-1&environment=prod&team=team-1&state=unavailable&runtime=offline&service=service-1&resource=target-1"))).toEqual({cluster: "cluster-1", environment: "prod", team: "team-1", state: "unavailable", runtime: "offline", service: "service-1", resource: "target-1"})
    expect(resourceFilters(new URLSearchParams("state=deleted")).state).toBe("all")
    expect(filterResources(data, {cluster: "all", environment: "prod", team: "all", state: "unbound", runtime: "offline", service: "all", resource: ""})).toEqual([data.resources[1]])
  })

  it("renders safe states and admin deep-link without edit forms", () => {
    const markup = renderToStaticMarkup(<MemoryRouter><ResourceWorkspaceView data={data} filters={{cluster: "all", environment: "all", team: "all", state: "all", runtime: "all", service: "all", resource: "target-1"}} /></MemoryRouter>)
    expect(markup).toContain("checkout-api")
    expect(markup).toContain("不可用")
    expect(markup).toContain("未绑定")
    expect(markup).toContain("cluster-idle")
    expect(markup).toContain("Read unverified")
    expect(markup).toContain("Worker")
    expect(markup).toContain("Inactive")
    expect(markup).toContain("/admin?section=catalog")
    expect(markup).toContain("resource=target-1")
    expect(markup).toContain('aria-current="true"')
    expect(markup).toContain("min-w-0")
    expect(markup).not.toContain("governance_notes")
    expect(markup).not.toContain("保存")
  })
})
