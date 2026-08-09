import type { components } from "@/api/schema"
import { request, write } from "@/api/transport"

export type NotificationDestination = components["schemas"]["NotificationDestination"]
export type NotificationSilence = components["schemas"]["NotificationSilence"]
export type NotificationRoute = components["schemas"]["NotificationRoute"]
export type NotificationSimulation = components["schemas"]["NotificationSimulation"]
export type NotificationTemplate = components["schemas"]["NotificationTemplate"]
export type NotificationTemplatePreview = components["schemas"]["NotificationTemplatePreview"]

export function getNotificationDestinations() {
  return request<components["schemas"]["NotificationDestinationListResponse"]>("/api/v1/admin/notification-destinations")
}

export function getNotificationRoutes() {
  return request<components["schemas"]["NotificationRouteListResponse"]>("/api/v1/admin/notification-routes")
}

export function createNotificationDestination(body: components["schemas"]["NotificationDestinationCreateRequest"], requestId?: string) {
  return write<components["schemas"]["NotificationDestinationResponse"]>("/api/v1/admin/notification-destinations", "POST", body, requestId)
}

export function updateNotificationDestination(id: string, body: components["schemas"]["NotificationDestinationUpdateRequest"], requestId?: string) {
  return write<components["schemas"]["NotificationDestinationResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}`, "PATCH", body, requestId)
}

export function testNotificationDestination(id: string, expectedRevision: string, reason: string) {
  return write<components["schemas"]["NotificationVerificationResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}/test`, "POST", {expected_revision: expectedRevision, reason})
}

export function selectNotificationPilotRoute(id: string, expectedRevision: string, reason: string) {
  return write<components["schemas"]["NotificationDestinationResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}/select-pilot-route`, "POST", {expected_revision: expectedRevision, reason})
}

export function updateNotificationNoiseControl(id: string, body: components["schemas"]["NotificationNoiseControlUpdateRequest"]) {
  return write<components["schemas"]["NotificationNoiseControlResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}/noise-control`, "PATCH", body)
}

export function getNotificationSilences() {
  return request<components["schemas"]["NotificationSilenceListResponse"]>("/api/v1/admin/notification-silences")
}

export function getNotificationDeliveries() {
  return request<components["schemas"]["NotificationDeliveryListResponse"]>("/api/v1/admin/notification-deliveries")
}

export function redeliverNotificationDelivery(id: string, reason: string) {
  return write<components["schemas"]["NotificationDeliveryResponse"]>(
    `/api/v1/admin/notification-deliveries/${encodeURIComponent(id)}/redeliver`, "POST", {reason},
  )
}

export function createNotificationSilence(body: components["schemas"]["NotificationSilenceCreateRequest"]) {
  return write<components["schemas"]["NotificationSilenceResponse"]>("/api/v1/admin/notification-silences", "POST", body)
}

export function createNotificationRoute(body: components["schemas"]["NotificationRouteCreateRequest"]) {
  return write<components["schemas"]["NotificationRouteResponse"]>("/api/v1/admin/notification-routes", "POST", body)
}

export function updateNotificationRoute(id: string, body: components["schemas"]["NotificationRouteUpdateRequest"]) {
  return write<components["schemas"]["NotificationRouteResponse"]>(`/api/v1/admin/notification-routes/${encodeURIComponent(id)}`, "PATCH", body)
}

export function simulateNotificationRoute(body: components["schemas"]["NotificationSimulationRequest"]) {
  return write<components["schemas"]["NotificationSimulationResponse"]>("/api/v1/admin/notification-routes/simulate", "POST", body)
}

export function getNotificationTemplates() {
  return request<components["schemas"]["NotificationTemplateListResponse"]>("/api/v1/admin/notification-templates")
}

export function copyNotificationTemplate(body: components["schemas"]["NotificationTemplateCopyRequest"]) {
  return write<components["schemas"]["NotificationTemplateResponse"]>("/api/v1/admin/notification-templates", "POST", body)
}

export function updateNotificationTemplate(id: string, body: components["schemas"]["NotificationTemplateUpdateRequest"]) {
  return write<components["schemas"]["NotificationTemplateResponse"]>(`/api/v1/admin/notification-templates/${encodeURIComponent(id)}`, "PATCH", body)
}

export function previewNotificationTemplate(id: string, body: components["schemas"]["NotificationTemplatePreviewRequest"]) {
  return write<components["schemas"]["NotificationTemplatePreviewResponse"]>(`/api/v1/admin/notification-templates/${encodeURIComponent(id)}/preview`, "POST", body)
}

export function testNotificationTemplate(id: string, body: components["schemas"]["NotificationTemplateTestRequest"]) {
  return write<components["schemas"]["NotificationTemplatePreviewResponse"]>(`/api/v1/admin/notification-templates/${encodeURIComponent(id)}/test`, "POST", body)
}
