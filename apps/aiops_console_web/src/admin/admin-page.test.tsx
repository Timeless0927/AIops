import { describe, expect, it } from "vitest"

import { adminDefaultSection } from "@/admin/admin-page"

describe("adminDefaultSection", () => {
  it("opens only the supported Resource Catalog deep-link", () => {
    expect(adminDefaultSection(new URLSearchParams("section=catalog"))).toBe("catalog")
    expect(adminDefaultSection(new URLSearchParams("section=secret"))).toBe("users")
  })
})
