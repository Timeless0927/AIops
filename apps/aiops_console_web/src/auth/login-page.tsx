import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ActivityIcon, LogInIcon } from "lucide-react"

import { login } from "@/api/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

export function LoginPage() {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const mutation = useMutation({
    mutationFn: () => login(username, password),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ["actor"]}),
  })
  const error = mutation.error instanceof Error ? mutation.error : null

  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <section className="w-full max-w-sm" aria-labelledby="login-title">
        <div className="mb-8 flex items-center gap-3">
          <span className="flex size-9 items-center justify-center rounded-md bg-foreground text-background">
            <ActivityIcon className="size-5" />
          </span>
          <div>
            <div className="text-sm font-medium">AIOps Control Plane</div>
            <div className="text-xs text-muted-foreground">本地身份认证</div>
          </div>
        </div>
        <h1 id="login-title" className="text-2xl font-semibold">登录</h1>
        <form
          className="mt-6 space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            mutation.mutate()
          }}
        >
          <label className="block space-y-2 text-sm font-medium">
            用户名
            <Input autoComplete="username" required value={username} onChange={(event) => setUsername(event.target.value)} />
          </label>
          <label className="block space-y-2 text-sm font-medium">
            密码
            <Input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
          </label>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>登录失败</AlertTitle>
              <AlertDescription>{error.message}</AlertDescription>
            </Alert>
          ) : null}
          <Button className="w-full" type="submit" disabled={mutation.isPending}>
            <LogInIcon />
            {mutation.isPending ? "正在登录" : "登录"}
          </Button>
        </form>
      </section>
    </main>
  )
}
