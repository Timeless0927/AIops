import type { components } from "@/api/schema"
import { request, write } from "@/api/transport"

export type AdminState = components["schemas"]["AdminStateResponse"]
export type AdminUser = components["schemas"]["AdminUser"]
export type AdminTeam = components["schemas"]["AdminTeam"]
export type AdminTeamMembership = components["schemas"]["AdminTeamMembership"]
export type AdminRoleBinding = components["schemas"]["AdminRoleBinding"]
export type KubernetesChangeAuthority = components["schemas"]["KubernetesChangeAuthority"]
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
export type MCPIntegration = components["schemas"]["MCPIntegration"]
export type MCPIntegrationCreate = components["schemas"]["MCPIntegrationCreateRequest"]
export type MCPIntegrationUpdate = components["schemas"]["MCPIntegrationUpdateRequest"]
export type Skill = components["schemas"]["Skill"]
export type SkillCreate = components["schemas"]["SkillCreateRequest"]
export type SkillVersionCreate = components["schemas"]["SkillVersionCreateRequest"]
export type AdminAuditEntry = components["schemas"]["AdminAuditEntry"]
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
export function getAdminState() {
  return request<AdminState>("/api/v1/admin/users")
}

export function getAdminAudit() {
  return request<components["schemas"]["AdminAuditResponse"]>("/api/v1/admin/audit")
    .then((response) => response.audit)
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

export function getMCPIntegrations() {
  return request<components["schemas"]["MCPIntegrationListResponse"]>(
    "/api/v1/admin/mcp-integrations",
  ).then((response) => response.mcp_integrations)
}

export function createMCPIntegration(body: MCPIntegrationCreate) {
  return write<components["schemas"]["MCPIntegrationResponse"]>(
    "/api/v1/admin/mcp-integrations", "POST", body,
  ).then((response) => response.mcp_integration)
}

export function updateMCPIntegration(id: string, body: MCPIntegrationUpdate) {
  return write<components["schemas"]["MCPIntegrationResponse"]>(
    `/api/v1/admin/mcp-integrations/${encodeURIComponent(id)}`, "PATCH", body,
  ).then((response) => response.mcp_integration)
}

export function verifyMCPIntegration(id: string, reason: string) {
  return write<components["schemas"]["MCPIntegrationResponse"]>(
    `/api/v1/admin/mcp-integrations/${encodeURIComponent(id)}/verify`, "POST", {reason},
  ).then((response) => response.mcp_integration)
}

export function getSkills() {
  return request<components["schemas"]["SkillListResponse"]>("/api/v1/admin/skills")
    .then((response) => response.skills)
}

export function createSkill(body: SkillCreate) {
  return write<components["schemas"]["SkillResponse"]>(
    "/api/v1/admin/skills", "POST", body,
  ).then((response) => response.skill)
}

export function createSkillVersion(id: string, body: SkillVersionCreate) {
  return write<components["schemas"]["SkillResponse"]>(
    `/api/v1/admin/skills/${encodeURIComponent(id)}/versions`, "POST", body,
  ).then((response) => response.skill)
}

export function enableSkill(
  id: string,
  version: number,
  expectedActiveVersion: number | null,
  reason: string,
) {
  return write<components["schemas"]["SkillResponse"]>(
    `/api/v1/admin/skills/${encodeURIComponent(id)}/enable`, "POST",
    {version, expected_active_version: expectedActiveVersion, reason},
  ).then((response) => response.skill)
}

export function disableSkill(id: string, expectedActiveVersion: number | null, reason: string) {
  return write<components["schemas"]["SkillResponse"]>(
    `/api/v1/admin/skills/${encodeURIComponent(id)}/disable`, "POST",
    {expected_active_version: expectedActiveVersion, reason},
  ).then((response) => response.skill)
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
