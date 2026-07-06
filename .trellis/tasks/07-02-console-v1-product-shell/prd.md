# Console V1 product shell and operations workflow

## Goal

Build Console V1 as a coherent operator-facing web product instead of a set of
rough static slices. The Console should let an SRE move from alert intake to
diagnosis review, approval, execution tracking, notification state, audit, and
history without leaving the Gateway-controlled UI.

## User Value

Operators need a clear place to answer:

- What is firing now?
- What did the agent inspect?
- What does it recommend?
- Does this require approval?
- Who approved or rejected it?
- What changed, when, and with what audit trail?
- What happened in previous incidents?

## Confirmed Facts

- The repository currently has a static `apps/aiops_console/static/incident-detail.html`
  slice with fixtures.
- Current Console styling is functional but not a product shell.
- Browser code must stay Gateway-only and never call Hermes, Connector, MCP,
  Prometheus, Loki, Feishu, or provider APIs directly.
- Gateway already owns auth/RBAC concepts and incident/writeback APIs.
- Active child tasks:
  - `07-02-console-v1-web-design-foundation`
  - `07-01-console-diagnosis-approval-slice`

## Requirements

1. Define the Console V1 information architecture before implementing all
   workflows.
2. Establish a visual design foundation that can support repeated operations
   work: dense, scannable, calm, and audit-friendly.
3. Keep implementation incremental through child tasks; do not start with a
   full frontend rewrite.
4. Preserve Gateway-only browser access and RBAC as non-negotiable constraints.
5. Cover the product areas:
   - login/session;
   - permission-aware navigation;
   - alert/incident list;
   - incident diagnosis process detail;
   - approval center;
   - notification center;
   - execution/audit history;
   - later constrained Agent follow-up actions.

## Acceptance Criteria

- Parent task records the Console V1 roadmap and child-task map.
- First child produces a usable web design foundation before deeper workflow
  implementation.
- Each workflow child has independently testable acceptance criteria.
- The roadmap keeps chat/follow-up Agent controls separate from first-pass
  diagnosis visibility.

## Out of Scope

- One-shot implementation of the entire Console.
- Browser-direct service calls outside Gateway.
- Free-form Agent chat as the first UI milestone.

## Child Task Map

Completed foundation children:

1. `07-02-console-v1-web-design-foundation`: product shell, navigation, visual
   system, representative static screens.
2. `07-01-console-diagnosis-approval-slice`: Gateway-backed diagnosis process
   detail after the design foundation is in place.
3. `07-03-07-03-console-gateway-auth-live-overview`: independent Web Pod,
   Gateway auth, and live overview.

Next implementation order:

1. `07-06-console-incident-workbench` (P1): complete the real incident and
   diagnosis review workbench. Can start now.
2. `07-06-console-approval-center` (P2): approval list/detail and decision UI.
   Can start after incident workbench API assumptions are stable.
3. `07-06-console-notification-center` (P2): Notification Center visibility.
   Can run in parallel with audit history; best after legacy notification
   migration for complete data.
4. `07-06-console-audit-history` (P2): timeline/audit/history read-only views.
   Can run in parallel with notification center.
5. `07-06-console-execution-tracking` (P2): execution lifecycle visibility.
   UI skeleton can start after approval center; real integration waits for
   `controlled-mutation-execution`.

Parallel lanes:

- Lane A: `console-incident-workbench` -> `console-approval-center` ->
  `console-execution-tracking`.
- Lane B: `notification-center-legacy-migration` ->
  `console-notification-center`.
- Lane C: `console-audit-history` can run once timeline/audit payloads are
  confirmed.
- Lane D: `real-fault-replay-expansion` is independent of Console UI.
- Defer `hermes-diagnosis-service-rename` until the Console and execution
  surfaces stop moving.
