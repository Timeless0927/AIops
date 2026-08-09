import type { components } from "@/api/schema"
import { request, write } from "@/api/transport"

export type IncidentReport = components["schemas"]["IncidentReportResponse"]
export type IncidentReportDraft = components["schemas"]["IncidentReportDraft"]
export type IncidentReportNarrative = components["schemas"]["IncidentReportNarrative"]
export type IncidentReportLibrarySummary = components["schemas"]["IncidentReportLibrarySummary"]

export function getIncidentReport(incidentId: string) {
  return request<IncidentReport>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/report`)
}

export function listIncidentReportLibrary() {
  return request<components["schemas"]["IncidentReportLibraryResponse"]>("/api/v1/reports")
}

export function updateIncidentReport(incidentId: string, body: IncidentReportNarrative) {
  return write<components["schemas"]["IncidentReportDraftResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/report`, "PATCH", body,
  )
}

export function publishIncidentReport(incidentId: string) {
  return write<components["schemas"]["IncidentReportPublicationResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/report/publish`, "POST", {},
  )
}
