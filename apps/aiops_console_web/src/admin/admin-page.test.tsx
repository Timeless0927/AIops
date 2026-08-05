import { describe, expect, it } from "vitest"

import { adminDefaultSection } from "@/admin/admin-page"

describe("adminDefaultSection", () => {
  it("opens only supported domain management deep-links", () => {
    expect(adminDefaultSection(new URLSearchParams("section=catalog"))).toBe("catalog")
    expect(adminDefaultSection(new URLSearchParams("section=model"))).toBe("model")
    expect(adminDefaultSection(new URLSearchParams("section=connectors"))).toBe("connectors")
    expect(adminDefaultSection(new URLSearchParams("section=notifications"))).toBe("notifications")
    expect(adminDefaultSection(new URLSearchParams("section=mcp"))).toBe("mcp")
    expect(adminDefaultSection(new URLSearchParams("section=secret"))).toBe("users")
  })
})
