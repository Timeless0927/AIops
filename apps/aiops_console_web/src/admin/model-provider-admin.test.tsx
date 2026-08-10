import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { ModelProviderAdminView } from "@/admin/model-provider-admin"


describe("ModelProviderAdminView", () => {
  it("renders masked exact-revision readiness and bounded management controls", () => {
    const markup = renderToStaticMarkup(
      <ModelProviderAdminView
        detail={{
          readiness: "ready",
          configuration_revision: "model-provider:revision-1",
          configuration: {
            endpoint: "https://models.example.test/v1",
            endpoint_scope: "external",
            model: "ops-model",
            timeout_seconds: 30,
            credential_configured: true,
          },
          verification: {
            operation_id: "verify:1",
            state: "verified",
            revision: "model-provider:revision-1",
            checked_at: 1_700_000_000,
            reason_code: null,
          },
          availability: {state: "available", observed_at: 1_700_000_000, reason_code: null},
        }}
        pending={false}
        error={null}
        onSave={vi.fn()}
        onTest={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(markup).toContain("ops-model")
    expect(markup).toContain("已验证")
    expect(markup).toContain("可用")
    expect(markup).toContain("external")
    expect(markup).toContain("编辑 Model Provider")
    expect(markup).toContain("测试")
    expect(markup).toContain("删除")
    expect(markup).toContain("min-w-0")
    expect(markup).not.toContain("secret-provider-key")
  })
})
