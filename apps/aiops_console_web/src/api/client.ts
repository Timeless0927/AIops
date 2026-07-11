import type { components } from "@/api/schema"

export type Actor = components["schemas"]["Actor"]
export type Incident = components["schemas"]["Incident"]
export type AdminState = components["schemas"]["AdminStateResponse"]
export type AdminUser = components["schemas"]["AdminUser"]
export type AdminTeam = components["schemas"]["AdminTeam"]
export type AdminTeamMembership = components["schemas"]["AdminTeamMembership"]
export type AdminRoleBinding = components["schemas"]["AdminRoleBinding"]
type UserCreateRequest = components["schemas"]["UserCreateRequest"]
type UserUpdateRequest = components["schemas"]["UserUpdateRequest"]
type TeamCreateRequest = components["schemas"]["TeamCreateRequest"]
type TeamUpdateRequest = components["schemas"]["TeamUpdateRequest"]
type TeamMembershipCreateRequest = components["schemas"]["TeamMembershipCreateRequest"]
type TeamMembershipUpdateRequest = components["schemas"]["TeamMembershipUpdateRequest"]
type RoleBindingCreateRequest = components["schemas"]["RoleBindingCreateRequest"]
type RoleBindingUpdateRequest = components["schemas"]["RoleBindingUpdateRequest"]
export type AdminMutation =
  | {resource: "users"; id?: string; body: UserCreateRequest | UserUpdateRequest}
  | {resource: "teams"; id?: string; body: TeamCreateRequest | TeamUpdateRequest}
  | {resource: "team-memberships"; id?: string; body: TeamMembershipCreateRequest | TeamMembershipUpdateRequest}
  | {resource: "role-bindings"; id?: string; body: RoleBindingCreateRequest | RoleBindingUpdateRequest}
type ActorResponse = components["schemas"]["ActorResponse"]
type IncidentListResponse = components["schemas"]["IncidentListResponse"]
type CsrfResponse = components["schemas"]["CsrfResponse"]

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
      "X-Request-ID": crypto.randomUUID(),
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

export function mutateAdmin({resource, id, body}: AdminMutation) {
  return write(
    `/api/v1/admin/${resource}${id ? `/${encodeURIComponent(id)}` : ""}`,
    id ? "PATCH" : "POST",
    body,
  )
}
