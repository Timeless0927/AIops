import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router"
import { describe, expect, it, vi } from "vitest"

import type { PlatformStatus } from "@/platform/platform-client"
import { PlatformStatusView } from "@/platform/platform-status-page"


const data: PlatformStatus = {
  request_id: "platform-status:1",
  generated_at: 1_700_000_010,
  capabilities: {
    model: {
      readiness: "ready", configuration: "present", configuration_revision: "model:1",
      setup_decision: "active",
      verification: {operation_id: "model-test:1", state: "verified", revision: "model:1", checked_at: 1_700_000_000, reason_code: null},
      availability: {state: "available", observed_at: 1_700_000_001, reason_code: null},
    },
    notification: {
      readiness: "not_ready", configuration: "present", configuration_revision: "notification:1",
      setup_decision: "active",
      verification: {operation_id: "notification-test:1", state: "failed", revision: "notification:1", checked_at: 1_700_000_002, reason_code: "authentication_failed"},
      availability: {state: "unavailable", observed_at: 1_700_000_002, reason_code: "authentication_failed"},
    },
    connector: {
      readiness: "not_ready", configuration: "present", configuration_revision: null,
      setup_decision: "active",
      verification: {operation_id: null, state: "verified", revision: null, checked_at: 1_700_000_003, reason_code: null},
      availability: {state: "degraded", observed_at: 1_700_000_004, reason_code: "connector_degraded"},
      connection: {states: ["offline", "online", "rotation_pending"], total: 3, online: 1},
    },
    observability: {
      readiness: "ready", configuration: "present", configuration_revision: null,
      setup_decision: "active",
      verification: {operation_id: null, state: "verified", revision: null, checked_at: 1_700_000_005, reason_code: null},
      availability: {state: "available", observed_at: 1_700_000_005, reason_code: null},
    },
  },
}

const actions = {
  onSelect: vi.fn(),
  onRefresh: vi.fn(),
  onVerify: vi.fn(),
  onSetupDecision: vi.fn(),
}

describe("PlatformStatusPage", () => {
  it("renders the selected capability rail and real recovery controls for administrators", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <PlatformStatusView
          data={data}
          selected="notification"
          canAdminister
          reason="repair Pilot integration"
          pending={false}
          error={null}
          {...actions}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("平台状态")
    expect(markup).toContain("模型提供方")
    expect(markup).toContain("通知目的地")
    expect(markup).toContain("Connector / Cluster")
    expect(markup).toContain("可观测性")
    expect(markup).toContain("鉴权失败")
    expect(markup).toContain("配置")
    expect(markup).toContain("验证 / 重试")
    expect(markup).toContain("暂时跳过")
    expect(markup).toContain('href="/admin?section=notifications"')
    expect(markup).toContain('href="/incidents"')
    expect(markup).toContain("事件工作区始终可进入")
    expect(markup).toContain("overflow-x-auto")
    expect(markup).toContain("overflow-x-hidden")
    expect(markup).toContain("min-w-[180px]")
    expect(markup).toContain('aria-current="true"')
    expect(markup).not.toContain("variant=rail")
    expect(markup).not.toContain("scenario=")
  })

  it("keeps ordinary users on a secret-free read-only summary", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <PlatformStatusView
          data={data}
          selected="notification"
          canAdminister={false}
          reason=""
          pending={false}
          error={null}
          {...actions}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("只读安全摘要")
    expect(markup).toContain("鉴权失败")
    expect(markup).not.toContain("暂时跳过")
    expect(markup).not.toContain("验证 / 重试")
    expect(markup).not.toContain("管理详细配置")
    expect(markup).not.toContain("reason")
    expect(markup).not.toContain("must-not-leak")
  })

  it("shows the connector connection mix without exposing owner details", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <PlatformStatusView
          data={data}
          selected="connector"
          canAdminister={false}
          reason=""
          pending={false}
          error={null}
          {...actions}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("1 / 3 在线")
    expect(markup).toContain("离线")
    expect(markup).toContain("凭据轮换中")
    expect(markup).toContain("部分 Connector 连接降级")
    expect(markup).not.toContain("cluster_id")
    expect(markup).not.toContain("connector_id")
  })

  it("does not offer skip after Notification is ready", () => {
    const ready = {
      ...data,
      capabilities: {
        ...data.capabilities,
        notification: {...data.capabilities.notification, readiness: "ready" as const},
      },
    }
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <PlatformStatusView
          data={ready}
          selected="notification"
          canAdminister
          reason="defer notifications"
          pending={false}
          error={null}
          {...actions}
        />
      </MemoryRouter>,
    )

    expect(markup).not.toContain("暂时跳过")
  })
})
