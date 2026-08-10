import { describe, expect, it } from "vitest"

import { formatRelativeTime } from "./utils"

const now = new Date("2026-08-10T12:00:00Z").getTime()
const at = (secondsAgo: number) => now / 1000 - secondsAgo

describe("formatRelativeTime", () => {
  it("covers every bucket", () => {
    expect(formatRelativeTime(at(5), now)).toBe("刚刚")
    expect(formatRelativeTime(at(59), now)).toBe("刚刚")
    expect(formatRelativeTime(at(60), now)).toBe("1 分钟前")
    expect(formatRelativeTime(at(59 * 60), now)).toBe("59 分钟前")
    expect(formatRelativeTime(at(60 * 60), now)).toBe("1 小时前")
    expect(formatRelativeTime(at(23 * 3600), now)).toBe("23 小时前")
    expect(formatRelativeTime(at(24 * 3600), now)).toBe("1 天前")
    expect(formatRelativeTime(at(29 * 24 * 3600), now)).toBe("29 天前")
  })

  it("falls back to a date for old timestamps and clamps future ones", () => {
    expect(formatRelativeTime(at(31 * 24 * 3600), now)).toBe(
      new Date(at(31 * 24 * 3600) * 1000).toLocaleDateString("zh-CN"),
    )
    expect(formatRelativeTime(at(-120), now)).toBe("刚刚")
  })
})
