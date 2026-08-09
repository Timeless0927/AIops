import type { components } from "@/api/schema"
import { newClientId, request, write } from "@/api/transport"

export type Incident = components["schemas"]["Incident"]
export type Workbench = components["schemas"]["WorkbenchResponse"]
export type RecommendedAction = components["schemas"]["RecommendedAction"]
export type InvestigationEvent = components["schemas"]["InvestigationEvent"]
export type InvestigationEventsPage = components["schemas"]["InvestigationEventsResponse"]
export type HumanInputRequest = components["schemas"]["HumanInputRequest"]

export function listIncidents() {
  return request<components["schemas"]["IncidentListResponse"]>("/api/v1/incidents")
    .then((response) => response.incidents)
}

export function getIncidentWorkbench(incidentId: string) {
  return request<Workbench>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/workbench`)
}

export async function listInvestigationEvents(investigationId: string, after = 0) {
  const events: InvestigationEvent[] = []
  let page: InvestigationEventsPage
  do {
    page = await request<InvestigationEventsPage>(
      `/api/v1/investigations/${encodeURIComponent(investigationId)}/events?after=${after}&limit=200`,
    )
    events.push(...page.events)
    after = page.next_cursor
  } while (page.has_more)
  return {...page, events}
}

export function submitHumanInput(investigationId: string, body: HumanInputRequest) {
  return write<components["schemas"]["InvestigationEventResponse"]>(
    `/api/v1/investigations/${encodeURIComponent(investigationId)}/human-input`,
    "POST",
    body,
  )
}

export function controlInvestigation(investigationId: string, action: "pause" | "takeover" | "terminate") {
  return write<components["schemas"]["InvestigationEventResponse"]>(
    `/api/v1/investigations/${encodeURIComponent(investigationId)}/controls`,
    "POST",
    {action, idempotency_key: newClientId()},
  )
}

export function reinvestigateIncident(incidentId: string) {
  return write<components["schemas"]["InvestigationResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/reinvestigate`,
    "POST",
    {idempotency_key: newClientId()},
  )
}
