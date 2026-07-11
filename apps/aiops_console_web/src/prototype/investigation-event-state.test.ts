import { afterEach, describe, expect, it, vi } from "vitest"

import { listInvestigationEvents, type InvestigationEvent } from "@/api/client"
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
  afterEach(() => vi.unstubAllGlobals())

  it("appends immutable events in cursor order without duplicates", () => {
    expect(appendInvestigationEvents([event(1, "original")], [event(2, "next"), event(1, "changed")])).toEqual([
      event(1, "original"),
      event(2, "next"),
    ])
  })

  it("loads every event page before handing history to the cache", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "r1", events: [event(200, "page one")], next_cursor: 200, has_more: true})))
      .mockResolvedValueOnce(new Response(JSON.stringify({request_id: "r2", events: [event(201, "page two")], next_cursor: 201, has_more: false})))
    vi.stubGlobal("fetch", fetch)

    const page = await listInvestigationEvents("investigation-1")

    expect(page.events.map(({id}) => id)).toEqual([200, 201])
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/investigations/investigation-1/events?after=200&limit=200", expect.anything())
  })
})
