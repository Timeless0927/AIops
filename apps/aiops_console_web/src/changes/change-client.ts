import type { components } from "@/api/schema"
import { newClientId, request, write } from "@/api/transport"

export type ChangeRequest = components["schemas"]["ChangeRequest"]
export type ChangeCenterSummary = components["schemas"]["ChangeCenterSummary"]
export type ChangeCenterDetail = Omit<components["schemas"]["ChangeCenterDetailResponse"], "request_id">
export type ChangeRequestCreate = components["schemas"]["ChangeRequestCreate"]
export type ChangeRequestInput = components["schemas"]["ChangeRequestInput"]
export type SecureInput = components["schemas"]["SecureInput"]
export type SecureInputCreate = components["schemas"]["SecureInputCreateRequest"]
export type KubernetesPhaseReview = components["schemas"]["KubernetesPhaseReview"]
export type KubernetesPhaseExecution = components["schemas"]["KubernetesPhaseExecution"]
export type KubernetesReconciliation = components["schemas"]["KubernetesReconciliation"]

export function createSecureInput(body: SecureInputCreate) {
  return write<components["schemas"]["SecureInputResponse"]>(
    "/api/v1/secure-inputs", "POST", body,
  ).then((response) => response.secure_input)
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

export function approveKubernetesPhase(changeRequestId: string, body: components["schemas"]["KubernetesPhaseApprovalRequest"]) {
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

export function startKubernetesPhaseExecution(changeRequestId: string, body: components["schemas"]["KubernetesPhaseExecutionStartRequest"]) {
  return write<components["schemas"]["KubernetesPhaseExecutionResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/start`,
    "POST", body,
  ).then((response) => response.phase_execution)
}

export function cancelKubernetesPhaseExecution(changeRequestId: string, body: components["schemas"]["KubernetesPhaseExecutionCancelRequest"]) {
  return write<components["schemas"]["KubernetesPhaseExecutionResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/cancel`,
    "POST", body,
  ).then((response) => response.phase_execution)
}

export function acceptKubernetesReconciliation(changeRequestId: string, body: components["schemas"]["KubernetesReconciliationAcceptanceRequest"]) {
  return write<components["schemas"]["KubernetesReconciliationResponse"]>(
    `/api/v1/change-requests/${encodeURIComponent(changeRequestId)}/phase-execution/reconciliation/accept`,
    "POST", body,
  ).then((response) => response.reconciliation)
}
