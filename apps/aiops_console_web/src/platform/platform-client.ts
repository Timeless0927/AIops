import type { components } from "@/api/schema"
import { request, write } from "@/api/transport"

export type PlatformStatus = components["schemas"]["PlatformStatusResponse"]
export type CapabilityStatus = components["schemas"]["CapabilityStatus"]

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
