import { createContext, useContext, useState, type ReactNode } from "react"

import { ApiError } from "@/api/transport"
import { reauthenticate } from "@/auth/auth-client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"

export type AdminAction = {
  title: string
  summary: string
  destructive?: boolean
  run: (reason: string) => Promise<unknown>
}

type ActionOutcome =
  | {status: "success"}
  | {status: "fresh-auth"; error: Error}
  | {status: "error"; stage: "authenticate" | "action"; error: Error}

function asError(error: unknown) {
  return error instanceof Error ? error : new Error("请求失败")
}

export async function executeAdminAction(
  action: AdminAction,
  reason: string,
  authentication?: {password: string; verify: (password: string) => Promise<unknown>},
): Promise<ActionOutcome> {
  if (authentication) {
    try {
      await authentication.verify(authentication.password)
    } catch (error) {
      return {status: "error", stage: "authenticate", error: asError(error)}
    }
  }
  try {
    await action.run(reason)
    return {status: "success"}
  } catch (error) {
    if (!authentication && error instanceof ApiError && error.code === "fresh_auth_required") {
      return {status: "fresh-auth", error}
    }
    return {status: "error", stage: "action", error: asError(error)}
  }
}

const AdminActionContext = createContext<((action: AdminAction) => void) | null>(null)

export function useAdminAction() {
  const request = useContext(AdminActionContext)
  if (!request) throw new Error("AdminActionProvider is missing")
  return request
}

export function AdminActionProvider({children}: {children: ReactNode}) {
  const [action, setAction] = useState<AdminAction | null>(null)
  const [reason, setReason] = useState("")
  const [password, setPassword] = useState("")
  const [needsAuthentication, setNeedsAuthentication] = useState(false)
  const [pending, setPending] = useState(false)
  const [retryExhausted, setRetryExhausted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = () => {
    setAction(null)
    setReason("")
    setPassword("")
    setNeedsAuthentication(false)
    setRetryExhausted(false)
    setError(null)
  }
  const request = (next: AdminAction) => {
    setAction(next)
    setReason("")
    setPassword("")
    setNeedsAuthentication(false)
    setRetryExhausted(false)
    setError(null)
  }
  const submit = async () => {
    if (!action || !reason.trim() || retryExhausted) return
    setPending(true)
    setError(null)
    const outcome = await executeAdminAction(
      action,
      reason.trim(),
      needsAuthentication ? {password, verify: reauthenticate} : undefined,
    )
    setPending(false)
    if (outcome.status === "success") {
      close()
    } else if (outcome.status === "fresh-auth") {
      setNeedsAuthentication(true)
      setError(outcome.error.message)
    } else {
      setError(outcome.error.message)
      if (outcome.stage === "action" && needsAuthentication) setRetryExhausted(true)
    }
  }

  return (
    <AdminActionContext.Provider value={request}>
      {children}
      <Dialog open={Boolean(action)} onOpenChange={(open) => { if (!open && !pending) close() }}>
        {action ? (
          <DialogContent>
            <form onSubmit={(event) => { event.preventDefault(); void submit() }}>
              <DialogHeader>
                <DialogTitle>{action.title}</DialogTitle>
                <DialogDescription>确认本次平台管理变更。</DialogDescription>
              </DialogHeader>
              <AdminActionFields
                action={action}
                reason={reason}
                password={password}
                needsAuthentication={needsAuthentication}
                pending={pending}
                retryExhausted={retryExhausted}
                error={error}
                onReasonChange={setReason}
                onPasswordChange={setPassword}
              />
              <DialogFooter className="mt-5">
                <Button type="button" variant="outline" disabled={pending} onClick={close}>取消</Button>
                <Button
                  type="submit"
                  variant={action.destructive ? "destructive" : "default"}
                  disabled={pending || retryExhausted || !reason.trim() || (needsAuthentication && !password)}
                >
                  {pending ? "正在执行" : needsAuthentication ? "重新认证并重试" : "确认执行"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        ) : null}
      </Dialog>
    </AdminActionContext.Provider>
  )
}

export function AdminActionFields({
  action,
  reason,
  password = "",
  needsAuthentication,
  pending,
  retryExhausted = false,
  error,
  onReasonChange = () => undefined,
  onPasswordChange = () => undefined,
}: {
  action: AdminAction
  reason: string
  password?: string
  needsAuthentication: boolean
  pending: boolean
  retryExhausted?: boolean
  error: string | null
  onReasonChange?: (value: string) => void
  onPasswordChange?: (value: string) => void
}) {
  return (
    <div className="mt-5 flex flex-col gap-4">
      <Alert variant={action.destructive ? "destructive" : "default"}>
        <AlertTitle>动作摘要</AlertTitle>
        <AlertDescription>{action.summary}</AlertDescription>
      </Alert>
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="admin-action-reason">变更原因</FieldLabel>
          <Input
            id="admin-action-reason"
            name="reason"
            value={reason}
            onChange={(event) => onReasonChange(event.target.value)}
            readOnly={needsAuthentication}
            required
            autoFocus={!needsAuthentication}
          />
        </Field>
        {needsAuthentication ? (
          <Field>
            <FieldLabel htmlFor="admin-action-password">当前密码</FieldLabel>
            <Input
              id="admin-action-password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => onPasswordChange(event.target.value)}
              required
              autoFocus
            />
          </Field>
        ) : null}
      </FieldGroup>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>{needsAuthentication && !retryExhausted ? "需要重新认证" : "变更未保存"}</AlertTitle>
          <AlertDescription>{error}{retryExhausted ? <p className="mt-2">原动作已重试一次，请取消后重新发起。</p> : null}</AlertDescription>
        </Alert>
      ) : null}
    </div>
  )
}
