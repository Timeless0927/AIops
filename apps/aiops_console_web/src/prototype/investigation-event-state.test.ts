import { describe, expect, it } from "vitest"

import type { InvestigationEvent } from "@/incidents/incident-client"
import { appendInvestigationEvents } from "./investigation-event-state"

const event = (id: number, content: string): InvestigationEvent => ({
  id,
  investigation_id: "investigation-1",
  type: "human_input.assertion",
  actor_id: "sre-1",
  payload: {content},
  created_at: id,
})

describe("Investigation Event cache", () => {
  it("appends immutable events in cursor order without duplicates", () => {
    expect(appendInvestigationEvents([event(1, "original")], [event(2, "next"), event(1, "changed")])).toEqual([
      event(1, "original"),
      event(2, "next"),
    ])
  })
})
