# Design: AIOps Console frontend redesign

## Scope

Redesign the current React/Vite app in `apps/aiops_console_web` around the
reference SRE workbench in `/tmp/aiops-sre-console.html`.

This design intentionally does not revive the old static `apps/aiops_console/`
slice. Current architecture, Docker packaging, and tests point to
`apps/aiops_console_web`.

## User Flow

1. Unauthenticated user sees a standalone Chinese login page.
2. Login uses the existing `POST /auth/login` flow.
3. Authenticated user lands on `总览`, a polished placeholder for the future
   large-screen operations dashboard.
4. User enters `事件工作台` from nav or overview CTA.
5. `事件工作台` is the faithful reference-style workbench for this pass.
6. `审批中心`, `通知中心`, and `审计历史` remain reachable and usable with the
   shared shell styling, but are not expanded to the same depth yet.

## Frontend Boundaries

- Keep React/Vite and existing build scripts.
- Keep all browser network calls relative to Gateway:
  - `/auth/login`
  - `/api/incidents/active`
  - `/api/incidents/{incident_id}/diagnosis-process`
  - `/api/approval-requests*`
  - `/api/notifications/*`
- Do not call Hermes, Connector, MCP, Prometheus, Loki, or Feishu directly.
- Do not add a demo-mode login bypass.
- Do not add new frontend dependencies.

## App Structure

Keep the current single-file React app for this pass. It is already one large
file, and splitting components now is not required to achieve the redesign.

Minimal component shape:

- `App`
  - owns token, actor, data loading, active view, and notices.
  - renders `LoginPage` when unauthenticated.
  - renders `ConsoleShell` when authenticated.
- `LoginPage`
  - standalone auth page using current username/password state and submit logic.
- `ConsoleShell`
  - compact sidebar, topbar, Chinese navigation, search/action controls.
  - routes by `activeView`.
- `OverviewDashboard`
  - placeholder home for the future large-screen dashboard.
  - shows key operational summary and CTA into `事件工作台`.
- `IncidentWorkbenchView`
  - reference-style three-column workbench.
  - left: overview metrics and incident queue.
  - center: incident headline, impact grid, diagnosis progress, evidence panels.
  - right: approval summary, agent/tool stream, audit feed.
- Existing `ApprovalCenterView`, `NotificationCenterView`, and
  `AuditHistoryView`
  - keep behavior, adjust classes only as needed for shared shell styling.

## Data Mapping

Real Gateway data wins wherever available.

Fallback content fills visual gaps only after authentication:

- Incident queue uses `incidents`; if fields are sparse, fill labels such as
  service, severity, age, impact, and tags with existing fallback values.
- Main incident headline uses `selectedIncident` plus `process.diagnosis`.
- Diagnosis progress uses `process.timeline`; fallback steps mirror the reference
  flow: confirm symptom, narrow impact, correlate root cause, wait for approval.
- Evidence panels use `process.evidence`; fallback renders lightweight metrics,
  logs, Kubernetes, and topology panels without adding a chart library.
- Approval panel uses `selectedApproval`; fallback copy describes a safe rollback
  recommendation without enabling mutation.
- Agent/tool stream and audit feed use existing diagnosis/action/audit refs where
  possible; fallback content is read-only narrative.

## Visual Direction

Use the reference as the source of truth for this pass:

- light operational console, not marketing page.
- compact sticky sidebar and topbar.
- dense cards/panels with small radii.
- status pills, metric cells, tables, progress rows, and audit feed.
- Chinese nav and headings.
- responsive collapse to one column under tablet width.

CSS remains hand-written in `src/styles.css`. Prefer CSS grid/flex and native
HTML controls over JavaScript layout logic.

## Compatibility

Existing tests in `tests/test_aiops_console_web.py` should continue to assert:

- React/Vite contract.
- Chinese-first UI copy.
- Gateway-only API calls.
- responsive layout.

Tests may be updated for intentional labels/layout changes:

- add `总览` and `事件工作台`.
- keep current labels for approval, notification, and audit.
- remove expectation that login is a sidebar card if such an assertion exists.

## Rollback

Rollback is limited to `apps/aiops_console_web/src/App.tsx`,
`apps/aiops_console_web/src/styles.css`, and the console web test file if updated.
No backend, API, deployment, or dependency changes are required.
