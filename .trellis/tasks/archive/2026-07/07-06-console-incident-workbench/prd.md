# Console incident workbench

## Goal

Turn the current Console home page into the primary incident workbench: active
incident list, selected incident detail, and full diagnosis-process review.

## Position

- Order: 1
- Priority: P1
- Parallel: can run now
- Depends on: existing Gateway incident and diagnosis-process APIs
- Blocks: approval center polish, execution tracking context

## Confirmed Facts

- Browser calls must stay Gateway-relative (`/auth/*`, `/api/*`).
- Current React app already calls `/api/incidents/active` and
  `/api/incidents/{id}/diagnosis-process`.
- The live cluster returns real incidents and diagnosis process data.

## Requirements

1. Replace demo-first behavior with a real incident workbench after login.
2. Show active incidents, severity/status/service/age, and empty/error states.
3. Show diagnosis summary, root cause, evidence counts, timeline/missing evidence,
   and action proposals in Chinese.
4. Keep demo fallback only for logged-out or API-failure states.
5. Do not add direct Hermes/Connector/MCP/Prometheus/Loki calls.

## Acceptance Criteria

- [ ] Logged-in users can refresh and inspect real active incidents.
- [ ] Selecting a real incident loads and displays diagnosis process data.
- [ ] UI remains usable when there are no active incidents or diagnosis is missing.
- [ ] `npm --prefix apps/aiops_console_web run build` passes.
- [ ] Gateway/Console smoke tests still pass.

## Out of Scope

- Approve/reject actions.
- Execute mutations.
- Notification and audit browsing.
