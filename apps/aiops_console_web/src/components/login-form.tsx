import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { LogInIcon } from "lucide-react"

import { login } from "@/auth/auth-client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

export function LoginForm({className, ...props}: React.ComponentProps<"form">) {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const mutation = useMutation({
    mutationFn: () => login(username, password),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ["actor"]}),
  })
  const error = mutation.error instanceof Error ? mutation.error : null

  return (
    <form
      className={className}
      {...props}
      onSubmit={(event) => {
        event.preventDefault()
        mutation.mutate()
      }}
    >
      <div className="space-y-4">
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
      </div>
    </form>
  )
}
