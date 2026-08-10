import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/transport"
import {
  AdminActionFields,
  executeAdminAction,
  type AdminAction,
} from "@/admin/admin-action"

const action = (run: AdminAction["run"]): AdminAction => ({
  title: "停用生产 Connector",
  summary: "Connector connector-prod 将停止接收新命令。",
  destructive: true,
  run,
})

describe("admin action coordinator", () => {
  it("reauthenticates after fresh_auth_required and retries the original action once", async () => {
    const reasons: string[] = []
    const run = vi.fn(async (reason: string) => {
      reasons.push(reason)
      if (reasons.length === 1) throw new ApiError(401, "fresh_auth_required", "请重新认证")
    })
    const verify = vi.fn(async () => undefined)
    const pending = action(run)

    expect(await executeAdminAction(pending, "计划内维护")).toMatchObject({status: "fresh-auth"})
    expect(await executeAdminAction(pending, "计划内维护", {password: "current-password", verify})).toEqual({status: "success"})
    expect(verify).toHaveBeenCalledOnce()
    expect(verify).toHaveBeenCalledWith("current-password")
    expect(reasons).toEqual(["计划内维护", "计划内维护"])
  })

  it("does not loop when the retried action still requires fresh authentication", async () => {
    const run = vi.fn(async () => {
      throw new ApiError(401, "fresh_auth_required", "请重新认证")
    })
    const verify = vi.fn(async () => undefined)
    const pending = action(run)

    await executeAdminAction(pending, "计划内维护")
    const result = await executeAdminAction(pending, "计划内维护", {password: "current-password", verify})

    expect(result).toMatchObject({status: "error", stage: "action"})
    expect(run).toHaveBeenCalledTimes(2)
    expect(verify).toHaveBeenCalledOnce()
  })
})

describe("AdminActionFields", () => {
  it("requires a reason and only reveals the current password after fresh auth is required", () => {
    const initial = renderToStaticMarkup(
      <AdminActionFields action={action(vi.fn())} reason="" needsAuthentication={false} pending={false} error={null} />,
    )
    const reauth = renderToStaticMarkup(
      <AdminActionFields action={action(vi.fn())} reason="计划内维护" needsAuthentication pending={false} error="密码错误" />,
    )

    expect(initial).toContain("Connector connector-prod 将停止接收新命令")
    expect(initial).toContain('name="reason"')
    expect(initial).toContain("required")
    expect(initial.match(/autofocus/g) ?? []).toHaveLength(1)
    expect(initial).not.toContain('name="password"')
    expect(initial).toContain("text-destructive")
    expect(reauth).toContain('name="password"')
    expect(reauth.match(/autofocus/g) ?? []).toHaveLength(1)
    expect(reauth).toContain("readOnly")
    expect(reauth).toContain("当前密码")
    expect(reauth).toContain("计划内维护")
    expect(reauth).toContain("密码错误")
  })
})
