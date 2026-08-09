import type { components } from "@/api/schema"
import { request, write } from "@/api/transport"

export type Actor = components["schemas"]["Actor"]
type ActorResponse = components["schemas"]["ActorResponse"]

export function getActor() {
  return request<ActorResponse>("/api/v1/actor").then((response) => response.actor)
}

export function login(username: string, password: string) {
  return request("/auth/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username, password, session_mode: "cookie"}),
  })
}

export async function logout() {
  await write("/auth/logout", "POST")
}

export function reauthenticate(password: string) {
  return write("/auth/reauth", "POST", {password})
}
