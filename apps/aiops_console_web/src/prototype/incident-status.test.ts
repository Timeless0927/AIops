import { describe, expect, it } from "vitest"

import { diagnosisStatusLabel } from "@/prototype/shared"

describe("diagnosisStatusLabel", () => {
  it.each([
    ["completed", "incomplete", "诊断证据不足"],
    ["partial", "incomplete", "诊断证据不足"],
    ["needs_human", "incomplete", "诊断需人工处理"],
    ["failed", null, "诊断失败"],
    ["diagnosed", "complete", "诊断已完成"],
    ["completed", null, "诊断证据不足"],
  ] as const)("maps %s/%s to %s", (outcome, gate, label) => {
    expect(diagnosisStatusLabel(outcome, gate)).toBe(label)
  })
})
