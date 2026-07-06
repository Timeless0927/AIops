# Redesign AIOps Console frontend

## Goal

Make `apps/aiops_console_web` feel like the provided SRE console reference in
`/tmp/aiops-sre-console.html`: a dense, operational incident workbench instead
of the current plain admin CRUD screen.

The redesign must preserve the existing Gateway-only browser boundary and the
current React/Vite app contract.

## Confirmed Facts

- Current production frontend is `apps/aiops_console_web`, a React/Vite app.
- The old `apps/aiops_console/` static slice directory exists but contains no
  files; current docs/tests point to `apps/aiops_console_web`.
- Browser calls must stay relative to Gateway routes: `/auth/*` and `/api/*`.
- Current tests assert Chinese-first copy, React/Vite build scripts, Gateway-only
  API calls, and responsive layout.
- Current UI is structurally simple: left navigation, login card, topbar,
  summary metrics, list/detail views for incidents, approvals, notifications,
  and audit.
- Reference UI uses a richer SRE workbench pattern: compact sidebar, sticky
  topbar, global search, three-column incident workspace, status pills, metrics,
  incident queue, central diagnosis/evidence/timeline panels, approval panel,
  agent/tool execution stream, and audit feed.

## Requirements

- Use the reference HTML as the visual and interaction direction, not as a
  separate static page.
- Treat the reference as the visual/layout source of truth for this pass:
  faithfully port its SRE workbench feel into the React app before inventing a
  different information architecture.
- Keep implementation in `apps/aiops_console_web`; do not revive the old static
  slice.
- Preserve the existing API contract and auth flow.
- Preserve existing product areas: incidents, approvals, notifications, audit.
- Use Chinese labels in the UI navigation. The logical areas are overview,
  incidents, approvals, notifications, and audit; user-facing labels should be
  Chinese.
- Prioritize the incident diagnosis workspace as the first screen.
- In this pass, make the default incident workspace the faithful reference port.
  Approvals, notifications, and audit should remain reachable and usable with
  the same shell/basic visual language, but do not need equivalent three-column
  depth yet.
- Use a standard login experience before the Console shell. Unauthenticated users
  should see a dedicated login page, not a workbench with an embedded login card.
- Do not add a demo-mode entry on the login page. Authentication stays as the
  existing Gateway login flow.
- After login, enter an overview dashboard home page first. For this pass it can
  be a polished placeholder for a future large-screen operations dashboard, with
  clear entry into the incident workbench.
- When Gateway data is sparse, show realistic demo/SRE narrative content to keep
  the authenticated workbench populated. Real Gateway fields win; fallback
  content fills missing diagnosis/evidence/progress/approval/agent/audit panels.
- Keep the diff small enough to review: reuse current React state/API code where
  possible; mainly replace layout/CSS and component structure.

## Acceptance Criteria

- [ ] Running `npm run build` in `apps/aiops_console_web` succeeds.
- [ ] Existing `tests/test_aiops_console_web.py` passes or is updated only for
      intentional visual-contract changes.
- [ ] The default incidents view resembles the reference workbench: sidebar,
      topbar/search/actions, incident queue, central diagnosis/evidence panels,
      right-side approval/execution/audit panels.
- [ ] Unauthenticated users see a standalone login page; the login form is not
      embedded as a sidebar card inside the operational workbench.
- [ ] Authenticated users land on an overview dashboard placeholder before
      entering the incident workbench.
- [ ] Navigation keeps the current product areas with Chinese labels:
      `总览`, `事件工作台`, `审批中心`, `通知中心`, `审计历史`.
- [ ] Approval, notification, and audit views remain reachable and usable.
- [ ] Browser still only calls relative `/auth/*` and `/api/*` Gateway routes.
- [ ] Login page has no separate demo-mode bypass.
- [ ] Mobile layout remains single-column without overlapping text or controls.
- [ ] No new frontend dependency is added unless the redesign cannot be done with
      existing React/CSS.

## Out Of Scope

- New backend APIs.
- Real charting libraries.
- Direct calls to Hermes, Connector, MCP, Prometheus, Loki, or Feishu.
- Production mutation execution.
- Full large-screen dashboard implementation; this pass only needs a credible
  placeholder home.

## Open Questions

- None blocking. Next planning step is to write `design.md` and `implement.md`
  for review before starting implementation.
