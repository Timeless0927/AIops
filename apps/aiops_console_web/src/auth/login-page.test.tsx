import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import { LoginPage } from "@/auth/login-page"

describe("LoginPage", () => {
  it("uses the existing username/password login contract without fake providers", () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <LoginPage />
      </QueryClientProvider>,
    )

    expect(markup).toContain("登录")
    expect(markup).toContain('autoComplete="username"')
    expect(markup).toContain('autoComplete="current-password"')
    expect(markup).not.toContain("Google")
    expect(markup).not.toContain("Apple")
    expect(markup).not.toContain('href="#"')
    expect(markup).not.toContain("注册")
  })
})
