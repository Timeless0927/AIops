import type { components } from "@/api/schema"

export type Actor = components["schemas"]["Actor"]
export type Incident = components["schemas"]["Incident"]
export type Workbench = components["schemas"]["WorkbenchResponse"]
export type IncidentReport = components["schemas"]["IncidentReportResponse"]
export type IncidentReportDraft = components["schemas"]["IncidentReportDraft"]
export type IncidentReportNarrative = components["schemas"]["IncidentReportNarrative"]
export type InvestigationEvent = components["schemas"]["InvestigationEvent"]
export type InvestigationEventsPage = components["schemas"]["InvestigationEventsResponse"]
export type HumanInputRequest = components["schemas"]["HumanInputRequest"]
export type AdminState = components["schemas"]["AdminStateResponse"]
export type AdminUser = components["schemas"]["AdminUser"]
export type AdminTeam = components["schemas"]["AdminTeam"]
export type AdminTeamMembership = components["schemas"]["AdminTeamMembership"]
export type AdminRoleBinding = components["schemas"]["AdminRoleBinding"]
export type ApprovalAuthority = components["schemas"]["ApprovalAuthority"]
export type ConnectorAdminState = components["schemas"]["ConnectorAdminStateResponse"]
export type ConnectorEnrollment = components["schemas"]["ConnectorEnrollment"]
export type Cluster = components["schemas"]["Cluster"]
export type ResourceCatalogState = components["schemas"]["ResourceCatalogStateResponse"]
export type DiscoveryCandidate = components["schemas"]["DiscoveryCandidate"]
export type CatalogService = components["schemas"]["CatalogService"]
export type NotificationDestination = components["schemas"]["NotificationDestination"]
export type NotificationSilence = components["schemas"]["NotificationSilence"]
export type NotificationRoute = components["schemas"]["NotificationRoute"]
export type NotificationSimulation = components["schemas"]["NotificationSimulation"]
export type NotificationTemplate = components["schemas"]["NotificationTemplate"]
export type NotificationTemplatePreview = components["schemas"]["NotificationTemplatePreview"]
type UserCreateRequest = components["schemas"]["UserCreateRequest"]
type UserUpdateRequest = components["schemas"]["UserUpdateRequest"]
type TeamCreateRequest = components["schemas"]["TeamCreateRequest"]
type TeamUpdateRequest = components["schemas"]["TeamUpdateRequest"]
type TeamMembershipCreateRequest = components["schemas"]["TeamMembershipCreateRequest"]
type TeamMembershipUpdateRequest = components["schemas"]["TeamMembershipUpdateRequest"]
type RoleBindingCreateRequest = components["schemas"]["RoleBindingCreateRequest"]
type RoleBindingUpdateRequest = components["schemas"]["RoleBindingUpdateRequest"]
type ConnectorEnrollmentCreateRequest = components["schemas"]["ConnectorEnrollmentCreateRequest"]
type ConnectorEnrollmentUpdateRequest = components["schemas"]["ConnectorEnrollmentUpdateRequest"]
type ClusterUpdateRequest = components["schemas"]["ClusterUpdateRequest"]
type ServiceCreateRequest = components["schemas"]["ServiceCreateRequest"]
type ResourceBindingCreateRequest = components["schemas"]["ResourceBindingCreateRequest"]
type ResourceBindingUpdateRequest = components["schemas"]["ResourceBindingUpdateRequest"]
type ApprovalAuthorityCreateRequest = components["schemas"]["ApprovalAuthorityCreateRequest"]
type ApprovalAuthorityUpdateRequest = components["schemas"]["ApprovalAuthorityUpdateRequest"]
export type AdminMutation =
  | {resource: "users"; id?: string; body: UserCreateRequest | UserUpdateRequest}
  | {resource: "teams"; id?: string; body: TeamCreateRequest | TeamUpdateRequest}
  | {resource: "team-memberships"; id?: string; body: TeamMembershipCreateRequest | TeamMembershipUpdateRequest}
  | {resource: "role-bindings"; id?: string; body: RoleBindingCreateRequest | RoleBindingUpdateRequest}
  | {resource: "connector-enrollments"; id?: string; body: ConnectorEnrollmentCreateRequest | ConnectorEnrollmentUpdateRequest}
  | {resource: "clusters"; id: string; body: ClusterUpdateRequest}
  | {resource: "services"; id?: never; body: ServiceCreateRequest}
  | {resource: "resource-bindings"; id?: string; body: ResourceBindingCreateRequest | ResourceBindingUpdateRequest}
  | {resource: "approval-authorities"; id?: string; body: ApprovalAuthorityCreateRequest | ApprovalAuthorityUpdateRequest}
type ActorResponse = components["schemas"]["ActorResponse"]
type IncidentListResponse = components["schemas"]["IncidentListResponse"]
type CsrfResponse = components["schemas"]["CsrfResponse"]
let requestSequence = 0

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId?: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Accept": "application/json",
      "X-Request-ID": globalThis.crypto?.randomUUID?.() ?? `req-${Date.now().toString(36)}-${(++requestSequence).toString(36)}`,
      ...init?.headers,
    },
  })
  const payload = await response.json()
  if (!response.ok) {
    throw new ApiError(
      response.status,
      payload.error?.code ?? "request_failed",
      payload.error?.message ?? "请求失败",
      payload.request_id,
    )
  }
  return payload as T
}

export function getActor() {
  return request<ActorResponse>("/api/v1/actor").then((response) => response.actor)
}

export function listIncidents() {
  return request<IncidentListResponse>("/api/v1/incidents").then((response) => response.incidents)
}

export function getIncidentWorkbench(incidentId: string) {
  return request<Workbench>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/workbench`)
}

export function getIncidentReport(incidentId: string) {
  return request<IncidentReport>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/report`)
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
    {action, idempotency_key: crypto.randomUUID()},
  )
}

export function reinvestigateIncident(incidentId: string) {
  return write<components["schemas"]["InvestigationResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/reinvestigate`,
    "POST",
    {idempotency_key: crypto.randomUUID()},
  )
}

export function login(username: string, password: string) {
  return request("/auth/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username, password, session_mode: "cookie"}),
  })
}

export async function logout() {
  const {csrf_token} = await request<CsrfResponse>("/auth/csrf")
  await request("/auth/logout", {method: "POST", headers: {"X-CSRF-Token": csrf_token}})
}

async function write<T>(path: string, method: "POST" | "PATCH", body: object) {
  const {csrf_token} = await request<CsrfResponse>("/auth/csrf")
  return request<T>(path, {
    method,
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf_token},
    body: JSON.stringify(body),
  })
}

export function reauthenticate(password: string) {
  return write("/auth/reauth", "POST", {password})
}

export function getAdminState() {
  return request<AdminState>("/api/v1/admin/users")
}

export function getConnectorAdminState() {
  return request<ConnectorAdminState>("/api/v1/admin/connector-enrollments")
}

export function getResourceCatalog() {
  return request<ResourceCatalogState>("/api/v1/admin/resource-catalog")
}

export function getApprovalAuthorities() {
  return request<components["schemas"]["ApprovalAuthorityListResponse"]>("/api/v1/admin/approval-authorities")
}

export function approveAndExecute(incidentId: string, actionId: string, actionVersion: number, actionHash: string) {
  return write<components["schemas"]["ApproveAndExecuteResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/actions/${encodeURIComponent(actionId)}/approve-and-execute`,
    "POST",
    {action_version: actionVersion, action_hash: actionHash, idempotency_key: crypto.randomUUID()},
  )
}

export function mutateAdmin({resource, id, body}: AdminMutation) {
  return write<{credential?: string}>(
    `/api/v1/admin/${resource}${id ? `/${encodeURIComponent(id)}` : ""}`,
    id ? "PATCH" : "POST",
    body,
  )
}

export function getNotificationDestinations() {
  return request<components["schemas"]["NotificationDestinationListResponse"]>("/api/v1/admin/notification-destinations")
}

export function getNotificationRoutes() {
  return request<components["schemas"]["NotificationRouteListResponse"]>("/api/v1/admin/notification-routes")
}

export function createNotificationDestination(body: components["schemas"]["NotificationDestinationCreateRequest"]) {
  return write<components["schemas"]["NotificationDestinationResponse"]>("/api/v1/admin/notification-destinations", "POST", body)
}

export function updateNotificationDestination(id: string, body: components["schemas"]["NotificationDestinationUpdateRequest"]) {
  return write<components["schemas"]["NotificationDestinationResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}`, "PATCH", body)
}

export function testNotificationDestination(id: string, reason: string) {
  return write<components["schemas"]["NotificationDestinationResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}/test`, "POST", {reason})
}

export function updateNotificationNoiseControl(id: string, body: components["schemas"]["NotificationNoiseControlUpdateRequest"]) {
  return write<components["schemas"]["NotificationNoiseControlResponse"]>(`/api/v1/admin/notification-destinations/${encodeURIComponent(id)}/noise-control`, "PATCH", body)
}

export function getNotificationSilences() {
  return request<components["schemas"]["NotificationSilenceListResponse"]>("/api/v1/admin/notification-silences")
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
