import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderToStaticMarkup } from "react-dom/server"
import { MemoryRouter } from "react-router"
import { describe, expect, it } from "vitest"

import {
  ReportLibraryListView,
  ReportLibraryPage,
  filterReportLibrary,
  reportLibraryFilters,
} from "@/reports/report-library-page"

const reports = [
  {
    incident: {
      id: "incident-1", title: "Checkout latency", severity: "critical" as const,
      lifecycle_state: "resolved" as const,
    },
    service: {id: "service-1", name: "Checkout"},
    state: "draft" as const,
    draft: {id: "draft-2", source_revision: 4, status: "draft" as const, updated_at: 4},
    latest_publication: {id: "report-1", version: 1, source_revision: 2, published_at: 2},
    publication_count: 1,
    relevant_at: 4,
  },
  {
    incident: {
      id: "incident-2", title: "Worker backlog", severity: "high" as const,
      lifecycle_state: "reopened" as const,
    },
    service: {id: "service-2", name: "Workers"},
    state: "reopened" as const,
    draft: null,
    latest_publication: {id: "report-2", version: 2, source_revision: 3, published_at: 3},
    publication_count: 2,
    relevant_at: 5,
  },
]

describe("ReportLibraryPage", () => {
  it("owns bounded state and Service filters in URL state", () => {
    expect(reportLibraryFilters(new URLSearchParams("state=draft&service=service-1"))).toEqual({
      state: "draft", service: "service-1", incident: "", time: "all",
    })
    expect(reportLibraryFilters(new URLSearchParams("state=secret&service=&incident=checkout&time=7d"))).toEqual({
      state: "all", service: "all", incident: "checkout", time: "7d",
    })
    expect(filterReportLibrary(reports, {
      state: "reopened", service: "service-2", incident: "worker", time: "all",
    })).toEqual([
      reports[1],
    ])
    expect(filterReportLibrary(reports, {
      state: "all", service: "all", incident: "", time: "24h",
    }, 90_000)).toEqual([])
  })

  it("renders summary-only rows linked to the existing Incident report route", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <ReportLibraryListView
          reports={reports}
          filters={{state: "all", service: "all", incident: "", time: "all"}}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("Checkout latency")
    expect(markup).toContain("最新版本 v1")
    expect(markup).toContain("2 个发布版本")
    expect(markup).toContain("/incidents/incident-1/report?from=reports")
    expect(markup).toContain("min-w-0")
    expect(markup).toContain("break-words")
    expect(markup).not.toContain("保存草稿")
    expect(markup).not.toContain("叙述内容")
  })

  it("renders a bounded empty state", () => {
    const markup = renderToStaticMarkup(
      <MemoryRouter>
        <ReportLibraryListView
          reports={[]}
          filters={{state: "all", service: "all", incident: "", time: "all"}}
        />
      </MemoryRouter>,
    )

    expect(markup).toContain("没有事件报告")
    expect(markup).toContain("min-h-64")
  })

  it("renders a stable loading state while the owner response is pending", () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter><ReportLibraryPage /></MemoryRouter>
      </QueryClientProvider>,
    )

    expect(markup).toContain('role="status"')
    expect(markup).toContain('aria-label="正在加载"')
    expect(markup).toContain("animate-pulse")
  })
})
