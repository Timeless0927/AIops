import { afterEach, describe, expect, it, vi } from "vitest"

import { newClientId } from "./transport"

describe("API transport", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("generates client IDs when randomUUID is unavailable on remote HTTP", () => {
    vi.stubGlobal("crypto", {})

    expect(newClientId()).toMatch(/^req-[a-z0-9]+-[a-z0-9]+$/)
  })
})
