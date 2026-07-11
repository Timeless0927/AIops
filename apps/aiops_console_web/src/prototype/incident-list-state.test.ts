import { describe, expect, it } from "vitest"

import { readIncidentListState, updateIncidentListSearch } from "./incident-list-state"

describe("incident list URL state", () => {
  it("uses stable defaults for missing or invalid parameters", () => {
    expect(readIncidentListState(new URLSearchParams("status=unknown"))).toEqual({
      filter: "active",
      query: "",
    })
  })

  it("preserves independent parameters while updating or clearing state", () => {
    const waiting = updateIncidentListSearch(new URLSearchParams("q=checkout"), "status", "waiting")
    expect(waiting.toString()).toBe("q=checkout&status=waiting")

    const active = updateIncidentListSearch(waiting, "status", "active")
    expect(active.toString()).toBe("q=checkout")
  })
})
