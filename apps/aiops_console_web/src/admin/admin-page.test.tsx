import { describe, expect, it } from "vitest"

import { adminDefaultSection } from "@/admin/admin-page"

describe("adminDefaultSection", () => {
  it("opens only supported domain management deep-links", () => {
    expect(adminDefaultSection(new URLSearchParams("section=users"))).toBe("users")
    expect(adminDefaultSection(new URLSearchParams("section=teams"))).toBe("teams")
    expect(adminDefaultSection(new URLSearchParams("section=memberships"))).toBe("memberships")
    expect(adminDefaultSection(new URLSearchParams("section=bindings"))).toBe("bindings")
    expect(adminDefaultSection(new URLSearchParams("section=kubernetes-authorities"))).toBe("kubernetes-authorities")
    expect(adminDefaultSection(new URLSearchParams("section=catalog"))).toBe("catalog")
    expect(adminDefaultSection(new URLSearchParams("section=clusters"))).toBe("clusters")
    expect(adminDefaultSection(new URLSearchParams("section=model"))).toBe("model")
    expect(adminDefaultSection(new URLSearchParams("section=connectors"))).toBe("connectors")
    expect(adminDefaultSection(new URLSearchParams("section=notifications"))).toBe("notifications")
    expect(adminDefaultSection(new URLSearchParams("section=mcp"))).toBe("mcp")
    expect(adminDefaultSection(new URLSearchParams("section=skills"))).toBe("skills")
    expect(adminDefaultSection(new URLSearchParams("section=secret"))).toBe("users")
  })
})
