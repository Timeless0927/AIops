import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { NotificationDestinationRepairForm, NotificationDestinationTable } from "@/admin/notification-admin"
import type { NotificationDestination } from "@/admin/notification-client"


function destination(
  id: string,
  state: NotificationDestination["verification"]["state"],
  selected = false,
): NotificationDestination {
  return {
    id,
    name: id,
    provider: "feishu",
    enabled: selected,
    config: {webhook_url: "https://open.feishu.cn/***"},
    noise_control: {timezone: "UTC", quiet_hours: null, hourly_limit: null, digest_interval_seconds: null},
    configuration_revision: `notification-destination-revision:${id}`,
    readiness: selected ? "ready" : "not_ready",
    verification: {
      operation_id: state === "unverified" ? null : `notification-delivery:${id}`,
      state,
      revision: `notification-destination-revision:${id}`,
      checked_at: state === "verified" ? 1_700_000_000 : null,
      reason_code: null,
    },
    availability: {
      state: state === "verified" ? "available" : "unavailable",
      observed_at: state === "verified" ? 1_700_000_000 : null,
      reason_code: state === "verified" ? null : "test_required",
    },
    pilot_route_selected: selected,
  }
}


describe("NotificationDestinationTable", () => {
  it("renders asynchronous exact-revision readiness and explicit Pilot Route selection", () => {
    const markup = renderToStaticMarkup(
      <NotificationDestinationTable
        destinations={[
          destination("verifying", "verifying"),
          destination("verified", "verified"),
          destination("ready", "verified", true),
        ]}
        reason="verify Pilot notification"
        pending={false}
        retryAtByOperation={{"notification-delivery:verifying": 1_700_000_045}}
        onTest={vi.fn()}
        onSelect={vi.fn()}
        onRepair={vi.fn()}
        onDisable={vi.fn()}
      />,
    )

    expect(markup).toContain("验证中")
    expect(markup).toContain("已验证")
    expect(markup).toContain("Pilot Ready")
    expect(markup).toContain("设为 Pilot Route")
    expect(markup).toContain("修复凭据")
    expect(markup).toContain("下次重试")
    expect(markup).toContain("https://open.feishu.cn/***")
    expect(markup).not.toContain("secret-token")
  })

  it("renders provider credential fields for an exact destination revision repair", () => {
    const item = {...destination("smtp", "failed"), provider: "smtp" as const}
    const markup = renderToStaticMarkup(
      <NotificationDestinationRepairForm
        destination={item}
        pending={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(markup).toContain("修复 smtp 凭据")
    expect(markup).toContain('name="password"')
    expect(markup).toContain('name="to_addresses"')
    expect(markup).toContain("保存新 revision")
  })
})
