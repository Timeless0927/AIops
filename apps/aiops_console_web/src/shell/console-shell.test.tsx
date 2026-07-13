import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter, Route, Routes } from "react-router"
import { describe, expect, it } from "vitest"

import type { Actor } from "@/api/client"
import { ConsoleShell, shellRoute } from "@/shell/console-shell"

const actor: Actor = {
  id: "user-1",
  username: "operator",
  display_name: "值班工程师",
  roles: ["sre"],
  capabilities: [],
  is_platform_administrator: false,
}

describe("ConsoleShell", () => {
  it("derives active navigation and back targets from the current route", () => {
    expect(shellRoute("/incidents")).toEqual({incidentsActive: true, changesActive: false})
    expect(shellRoute("/incidents/")).toEqual({incidentsActive: true, changesActive: false})
    expect(shellRoute("/incidents/inc-1")).toEqual({
      incidentsActive: true,
      changesActive: false,
      backTo: "/incidents",
      backLabel: "返回事件列表",
    })
    expect(shellRoute("/incidents/inc-1/report")).toEqual({
      incidentsActive: true,
      changesActive: false,
      backTo: "/incidents/inc-1",
      backLabel: "返回事件工作区",
    })
    expect(shellRoute("/changes")).toEqual({incidentsActive: false, changesActive: true})
    expect(shellRoute("/changes/change-1")).toEqual({
      incidentsActive: false,
      changesActive: true,
      backTo: "/changes",
      backLabel: "返回变更列表",
    })
    expect(shellRoute("/admin")).toEqual({incidentsActive: false, changesActive: false})
  })

  it("renders focusable named controls without a 390px overflow path", () => {
    const queryClient = new QueryClient()
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/incidents/inc-1/report"]}>
          <Routes>
            <Route element={<ConsoleShell actor={actor} />}>
              <Route path="/incidents/:incidentId/report" element={<main>报告</main>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(markup).toContain('aria-current="page"')
    expect(markup).toContain('aria-label="返回事件工作区"')
    expect(markup).toContain('aria-label="打开主导航"')
    expect(markup).toContain("sm:hidden")
    expect(markup).toContain('aria-label="用户菜单"')
    expect(markup).toContain('href="/changes"')
    expect(markup).toContain("变更")
    expect(markup).toContain("focus-visible:ring-2")
    expect(markup).toContain("overflow-x-hidden")
    expect(markup).toContain("min-w-0")
    expect(markup).toContain("shrink-0")
    expect(markup).not.toContain('tabindex="-1"')
  })
})
