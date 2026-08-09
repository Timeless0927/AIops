import type { components } from "@/api/schema"
import { request } from "@/api/transport"

export type ResourceWorkspace = components["schemas"]["ResourceWorkspaceResponse"]

export function listResourceWorkspace() {
  return request<ResourceWorkspace>("/api/v1/resources")
}
