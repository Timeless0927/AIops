import { describe, expect, it } from "vitest"

import { resolveAdminRoute } from "@/admin/admin-page"

describe("resolveAdminRoute", () => {
  it("defaults to the overview section", () => {
    expect(resolveAdminRoute(new URLSearchParams())).toEqual({section: "overview", tab: ""})
    expect(resolveAdminRoute(new URLSearchParams("section=secret"))).toEqual({section: "overview", tab: ""})
  })

  it("resolves canonical section and tab pairs", () => {
    expect(resolveAdminRoute(new URLSearchParams("section=identity&tab=bindings"))).toEqual({section: "identity", tab: "bindings"})
    expect(resolveAdminRoute(new URLSearchParams("section=access&tab=clusters"))).toEqual({section: "access", tab: "clusters"})
    expect(resolveAdminRoute(new URLSearchParams("section=ai&tab=mcp"))).toEqual({section: "ai", tab: "mcp"})
    expect(resolveAdminRoute(new URLSearchParams("section=catalog"))).toEqual({section: "catalog", tab: ""})
  })

  it("falls back to the first tab for unknown tabs", () => {
    expect(resolveAdminRoute(new URLSearchParams("section=identity"))).toEqual({section: "identity", tab: "users"})
    expect(resolveAdminRoute(new URLSearchParams("section=identity&tab=nope"))).toEqual({section: "identity", tab: "users"})
  })

  it("maps legacy deep-links into the grouped sections", () => {
    expect(resolveAdminRoute(new URLSearchParams("section=users"))).toEqual({section: "identity", tab: "users"})
    expect(resolveAdminRoute(new URLSearchParams("section=teams"))).toEqual({section: "identity", tab: "teams"})
    expect(resolveAdminRoute(new URLSearchParams("section=memberships"))).toEqual({section: "identity", tab: "memberships"})
    expect(resolveAdminRoute(new URLSearchParams("section=bindings"))).toEqual({section: "identity", tab: "bindings"})
    expect(resolveAdminRoute(new URLSearchParams("section=kubernetes-authorities"))).toEqual({section: "identity", tab: "kubernetes-authorities"})
    expect(resolveAdminRoute(new URLSearchParams("section=connectors"))).toEqual({section: "access", tab: "connectors"})
    expect(resolveAdminRoute(new URLSearchParams("section=clusters"))).toEqual({section: "access", tab: "clusters"})
    expect(resolveAdminRoute(new URLSearchParams("section=catalog"))).toEqual({section: "catalog", tab: ""})
    expect(resolveAdminRoute(new URLSearchParams("section=model"))).toEqual({section: "ai", tab: "model"})
    expect(resolveAdminRoute(new URLSearchParams("section=mcp"))).toEqual({section: "ai", tab: "mcp"})
    expect(resolveAdminRoute(new URLSearchParams("section=skills"))).toEqual({section: "ai", tab: "skills"})
    expect(resolveAdminRoute(new URLSearchParams("section=notifications"))).toEqual({section: "notifications", tab: ""})
  })
})
