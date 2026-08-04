import type { components } from "@/api/schema"

export type Actor = components["schemas"]["Actor"]
export type Incident = components["schemas"]["Incident"]
export type Workbench = components["schemas"]["WorkbenchResponse"]
export type RecommendedAction = components["schemas"]["RecommendedAction"]
export type ChangeRequest = components["schemas"]["ChangeRequest"]
export type ChangeCenterSummary = components["schemas"]["ChangeCenterSummary"]
export type ChangeCenterDetail = Omit<components["schemas"]["ChangeCenterDetailResponse"], "request_id">
export type ChangeRequestCreate = components["schemas"]["ChangeRequestCreate"]
export type ChangeRequestInput = components["schemas"]["ChangeRequestInput"]
export type SecureInput = components["schemas"]["SecureInput"]
export type SecureInputCreate = components["schemas"]["SecureInputCreateRequest"]
export type IncidentReport = components["schemas"]["IncidentReportResponse"]
export type IncidentReportDraft = components["schemas"]["IncidentReportDraft"]
export type IncidentReportNarrative = components["schemas"]["IncidentReportNarrative"]
export type IncidentReportLibrarySummary = components["schemas"]["IncidentReportLibrarySummary"]
export type ResourceWorkspace = components["schemas"]["ResourceWorkspaceResponse"]
export type InvestigationEvent = components["schemas"]["InvestigationEvent"]
export type InvestigationEventsPage = components["schemas"]["InvestigationEventsResponse"]
export type HumanInputRequest = components["schemas"]["HumanInputRequest"]
export type AdminState = components["schemas"]["AdminStateResponse"]
export type AdminUser = components["schemas"]["AdminUser"]
export type AdminTeam = components["schemas"]["AdminTeam"]
export type AdminTeamMembership = components["schemas"]["AdminTeamMembership"]
export type AdminRoleBinding = components["schemas"]["AdminRoleBinding"]
export type KubernetesChangeAuthority = components["schemas"]["KubernetesChangeAuthority"]
export type KubernetesPhaseReview = components["schemas"]["KubernetesPhaseReview"]
export type KubernetesPhaseExecution = components["schemas"]["KubernetesPhaseExecution"]
export type KubernetesReconciliation = components["schemas"]["KubernetesReconciliation"]
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
export type ModelProviderDetail = components["schemas"]["ModelProviderDetail"]
export type ModelProviderSave = components["schemas"]["ModelProviderSaveRequest"]
export type PlatformStatus = components["schemas"]["PlatformStatusResponse"]
export type CapabilityStatus = components["schemas"]["CapabilityStatus"]
export type ChatSession = components["schemas"]["ChatSession"]
export type ChatSessionSummary = components["schemas"]["ChatSessionSummary"]
export type ChatEvent = components["schemas"]["ChatEvent"]
export type ChatScopeSelection = components["schemas"]["ChatScopeSelection"]
export type ChatHandoff = components["schemas"]["ChatHandoff"]
export type ChatHandoffTarget = components["schemas"]["ChatHandoffRequest"]["target"]
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
export type AdminMutation =
  | {resource: "users"; id?: string; body: UserCreateRequest | UserUpdateRequest}
  | {resource: "teams"; id?: string; body: TeamCreateRequest | TeamUpdateRequest}
  | {resource: "team-memberships"; id?: string; body: TeamMembershipCreateRequest | TeamMembershipUpdateRequest}
  | {resource: "role-bindings"; id?: string; body: RoleBindingCreateRequest | RoleBindingUpdateRequest}
  | {resource: "connector-enrollments"; id?: string; body: ConnectorEnrollmentCreateRequest | ConnectorEnrollmentUpdateRequest}
  | {resource: "clusters"; id: string; body: ClusterUpdateRequest}
  | {resource: "services"; id?: never; body: ServiceCreateRequest}
  | {resource: "resource-bindings"; id?: string; body: ResourceBindingCreateRequest | ResourceBindingUpdateRequest}
type ActorResponse = components["schemas"]["ActorResponse"]
type IncidentListResponse = components["schemas"]["IncidentListResponse"]
type CsrfResponse = components["schemas"]["CsrfResponse"]
let requestSequence = 0

export function newClientId() {
  return globalThis.crypto?.randomUUID?.() ?? `req-${Date.now().toString(36)}-${(++requestSequence).toString(36)}`
}

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
      "X-Request-ID": newClientId(),
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

export function getPlatformStatus() {
  return request<PlatformStatus>("/api/v1/platform/status")
}

export function setNotificationSetupDecision(
  setupDecision: "active" | "skipped",
  expectedRevision: string | null,
  reason: string,
) {
  return write<components["schemas"]["PlatformSetupDecisionResponse"]>(
    "/api/v1/admin/platform/capabilities/notification/setup-decision",
    "PUT",
    {setup_decision: setupDecision, expected_revision: expectedRevision, reason},
  ).then((response) => response.setup_decision)
}

export function listIncidents() {
  return request<IncidentListResponse>("/api/v1/incidents").then((response) => response.incidents)
}

export function listChatSessions() {
  return request<components["schemas"]["ChatSessionListResponse"]>("/api/v1/chat/sessions")
    .then((response) => response.chat_sessions)
}

export function createChatSession() {
  return write<components["schemas"]["ChatSessionResponse"]>(
    "/api/v1/chat/sessions", "POST", {idempotency_key: newClientId()},
  ).then((response) => response.chat_session)
}

export function getChatSession(sessionId: string) {
  return request<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`,
  ).then((response) => response.chat_session)
}

export function sendChatMessage(
  sessionId: string,
  content: string,
  idempotencyKey: string = newClientId(),
  scope?: ChatScopeSelection,
) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    "POST",
    {content, idempotency_key: idempotencyKey, ...(scope ? {scope} : {})},
  ).then((response) => response.chat_session)
}

export function retryChatMessage(sessionId: string, messageId: string) {
  return write<components["schemas"]["ChatSessionResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/retry`,
    "POST",
    {},
  ).then((response) => response.chat_session)
}

export function createChatHandoff(
  sessionId: string,
  messageIds: string[],
  target: ChatHandoffTarget,
  idempotencyKey: string = newClientId(),
) {
  return write<components["schemas"]["ChatHandoffResponse"]>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/handoffs`,
    "POST",
    {message_ids: messageIds, idempotency_key: idempotencyKey, target},
  ).then((response) => response.handoff)
}

export function createSecureInput(body: SecureInputCreate) {
  return write<components["schemas"]["SecureInputResponse"]>(
    "/api/v1/secure-inputs", "POST", body,
  ).then((response) => response.secure_input)
}

export function getIncidentWorkbench(incidentId: string) {
  return request<Workbench>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/workbench`)
}

export function listChangeCenter() {
  return request<components["schemas"]["ChangeCenterListResponse"]>("/api/v1/changes")
}

export function getChangeCenterDetail(changeRequestId: string) {
  return request<components["schemas"]["ChangeCenterDetailResponse"]>(
    `/api/v1/changes/${encodeURIComponent(changeRequestId)}`,
  ).then(({request_id: _requestId, ...detail}) => detail)
}

export function createChangeRequest(incidentId: string, body: ChangeRequestCreate) {
  return write<components["schemas"]["ChangeRequestResponse"]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/change-requests`, "POST", body,
  )
}

export function submitChangeRequestInput(changeRequestId: string, body: ChangeRequestInput) {
  return write<components["schemas"]["ChangeRequestResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/input`, "POST", body,
  )
}

export function retryChangeRequestPlanning(changeRequestId: string) {
  return write<components["schemas"]["ChangeRequestResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/retry`, "POST", {idempotency_key: newClientId()},
  )
}

export function getKubernetesPhaseReview(changeRequestId: string) {
  return request<components["schemas"]["KubernetesPhaseReviewResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-approval`,
  ).then((response) => response.phase_review)
}

export function approveKubernetesPhase(
  changeRequestId: string,
  body: components["schemas"]["KubernetesPhaseApprovalRequest"],
) {
  return write<components["schemas"]["KubernetesPhaseReviewResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-approval/approve`,
    "POST",
    body,
  ).then((response) => response.phase_review)
}

export function getKubernetesPhaseExecution(changeRequestId: string) {
  return request<components["schemas"]["KubernetesPhaseExecutionResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution`,
  ).then((response) => response.phase_execution)
}

export function startKubernetesPhaseExecution(
  changeRequestId: string,
  body: components["schemas"]["KubernetesPhaseExecutionStartRequest"],
) {
  return write<components["schemas"]["KubernetesPhaseExecutionResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/start`,
    "POST", body,
  ).then((response) => response.phase_execution)
}

export function cancelKubernetesPhaseExecution(
  changeRequestId: string,
  body: components["schemas"]["KubernetesPhaseExecutionCancelRequest"],
) {
  return write<components["schemas"]["KubernetesPhaseExecutionResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/cancel`,
    "POST", body,
  ).then((response) => response.phase_execution)
}

export function acceptKubernetesReconciliation(
  changeRequestId: string,
  body: components["schemas"]["KubernetesReconciliationAcceptanceRequest"],
) {
  return write<components["schemas"]["KubernetesReconciliationResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/reconciliation/accept`,
    "POST", body,
  ).then((response) => response.reconciliation)
}

export function getIncidentReport(incidentId: string) {
  return request<IncidentReport>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/report`)
}

export function listIncidentReportLibrary() {
  return request<components["schemas"]["IncidentReportLibraryResponse"]>("/api/v1/reports")
}

export function listResourceWorkspace() {
  return request<ResourceWorkspace>("/api/v1/resources")
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

async function write<T>(path: string, method: "POST" | "PATCH" | "PUT" | "DELETE", body: object, requestId?: string) {
  const {csrf_token} = await request<CsrfResponse>("/auth/csrf")
  return request<T>(path, {
    method,
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf_token, ...(requestId ? {"X-Request-ID": requestId} : {})},
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

export function getModelProviderDetail() {
  return request<components["schemas"]["ModelProviderDetailResponse"]>(
    "/api/v1/admin/model-provider",
  ).then((response) => response.model_provider)
}

export function saveModelProvider(body: ModelProviderSave) {
  return write<components["schemas"]["ModelProviderDetailResponse"]>(
    "/api/v1/admin/model-provider", "PUT", body,
  ).then((response) => response.model_provider)
}

export function testModelProvider(expectedRevision: string, reason: string) {
  return write<components["schemas"]["ModelProviderVerificationResponse"]>(
    "/api/v1/admin/model-provider/test", "POST", {expected_revision: expectedRevision, reason},
  ).then((response) => response.verification)
}

export function deleteModelProvider(expectedRevision: string, reason: string) {
  return write<components["schemas"]["ModelProviderDetailResponse"]>(
    "/api/v1/admin/model-provider", "DELETE", {expected_revision: expectedRevision, reason},
  ).then((response) => response.model_provider)
}

export function getKubernetesChangeAuthorities() {
  return request<components["schemas"]["KubernetesChangeAuthorityListResponse"]>(
    "/api/v1/admin/kubernetes-change-authorities",
  ).then((response) => response.kubernetes_change_authorities)
}

export function createKubernetesChangeAuthority(
  body: components["schemas"]["KubernetesChangeAuthorityCreateRequest"],
) {
  return write<components["schemas"]["KubernetesChangeAuthorityResponse"]>(
    "/api/v1/admin/kubernetes-change-authorities", "POST", body,
  ).then((response) => response.kubernetes_change_authority)
}

export function updateKubernetesChangeAuthority(
  id: string,
  body: components["schemas"]["KubernetesChangeAuthorityUpdateRequest"],
) {
  return write<components["schemas"]["KubernetesChangeAuthorityResponse"]>(
    `/api/v1/admin/kubernetes-change-authorities/${encodeURIComponent(id)}`, "PATCH", body,
  ).then((response) => response.kubernetes_change_authority)
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
