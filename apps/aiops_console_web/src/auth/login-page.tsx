import { ActivityIcon } from "lucide-react"

import { LoginForm } from "@/components/login-form"
import { Card, CardContent } from "@/components/ui/card"

export function LoginPage() {
  return (
    <main className="relative grid min-h-screen place-items-center overflow-hidden px-4 py-10">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-32 left-1/2 h-80 w-[36rem] -translate-x-1/2 rounded-full bg-evidence/10 blur-3xl dark:bg-evidence/15"
      />
      <Card className="relative w-full max-w-sm" aria-labelledby="login-title">
        <CardContent className="grid gap-6">
          <div className="flex items-center gap-3">
            <span className="flex size-9 items-center justify-center rounded-md bg-foreground text-background">
              <ActivityIcon className="size-5" />
            </span>
            <div>
              <div className="text-sm font-medium">AIOps 控制台</div>
              <div className="text-xs text-muted-foreground">本地身份认证</div>
            </div>
          </div>
          <h1 id="login-title" className="text-2xl font-semibold">登录</h1>
          <LoginForm />
        </CardContent>
      </Card>
    </main>
  )
}
