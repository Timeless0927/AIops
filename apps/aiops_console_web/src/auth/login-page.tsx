import { ActivityIcon } from "lucide-react"

import { LoginForm } from "@/components/login-form"

export function LoginPage() {
  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <section className="w-full max-w-sm" aria-labelledby="login-title">
        <div className="mb-8 flex items-center gap-3">
          <span className="flex size-9 items-center justify-center rounded-md bg-foreground text-background">
            <ActivityIcon className="size-5" />
          </span>
          <div>
            <div className="text-sm font-medium">AIOps 控制台</div>
            <div className="text-xs text-muted-foreground">本地身份认证</div>
          </div>
        </div>
        <h1 id="login-title" className="text-2xl font-semibold">登录</h1>
        <LoginForm className="mt-6" />
      </section>
    </main>
  )
}
