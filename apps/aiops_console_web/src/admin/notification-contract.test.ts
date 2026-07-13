import { describe, expect, it } from "vitest"

import {
  notificationEvents,
  notificationSimulationFacts,
  notificationSubjectType,
} from "@/admin/notification-admin"
import { sampleRequest } from "@/admin/notification-template-admin"

describe("Generic Change notification consumers", () => {
  it("exposes only Change events and their exact simulation facts", () => {
    expect(notificationEvents).toContain("change.effect_observed")
    expect(notificationEvents).toContain("change.reconciliation_accepted")
    expect(notificationEvents.some((event) => event.startsWith("approval."))).toBe(false)
    expect(notificationEvents.some((event) => event.startsWith("execution."))).toBe(false)
    expect(notificationSubjectType("change.effect_observed")).toBe("change_request")
    expect(notificationSimulationFacts("change.effect_observed")).toEqual({
      incident_id: "simulation",
      change_request_id: "simulation",
      phase_id: "simulation",
      reconciliation_id: "simulation",
      status: "effect_observed",
    })
  })

  it("builds template previews with the Change Request subject contract", () => {
    const request = sampleRequest("change.reconciliation_accepted")
    expect(request.subject.type).toBe("change_request")
    expect(request.facts).toMatchObject({
      change_request_id: "preview",
      phase_id: "preview",
      reconciliation_id: "preview",
    })
  })
})
