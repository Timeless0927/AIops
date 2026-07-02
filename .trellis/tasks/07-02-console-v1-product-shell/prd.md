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

1. `07-02-console-v1-web-design-foundation`: product shell, navigation, visual
   system, representative static screens.
2. `07-01-console-diagnosis-approval-slice`: Gateway-backed diagnosis process
   detail after the design foundation is in place.
3. Future child: login/session and permission-aware shell.
4. Future child: incident list and history.
5. Future child: approval center.
6. Future child: notification center.
7. Future child: constrained Agent follow-up actions.
