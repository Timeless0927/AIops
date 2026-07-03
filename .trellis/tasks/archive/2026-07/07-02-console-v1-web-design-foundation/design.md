# Design: Console V1 web foundation

## Product Direction

Console V1 should feel like an operations workbench: dense, calm, readable, and
trustworthy. It should prioritize fast triage, approval awareness, and
auditability over display-wall visuals or generic admin forms.

## Information Architecture

Primary navigation:

- Overview
- Incidents
- Diagnosis
- Approvals
- Notifications
- Executions
- Audit
- Settings

The navigation is the complete product skeleton. Non-MVP areas should be shown
as disabled `planned` entries; their placement and naming should be settled in
this foundation without adding placeholder pages.

First static screens:

- `console-shell.html`: Overview dashboard and navigation baseline.
- Incident list/work queue section inside Overview; no dedicated incident list
  page in this foundation.
- `incident-detail.html`: upgraded diagnosis process detail using the shared
  shell style.
- Approval Center preview is a read-only panel inside Overview; no separate
  approval page in this foundation.

## Reference Baseline

Use `/tmp/aiops-sre-console.html` as the design reference, not as code to copy
wholesale. Preserve its product structure:

- left navigation and policy/status summary;
- top bar with title, search/context, and read-only actions;
- left column for incident metrics and active incident work queue;
- center column for selected incident summary, diagnosis progress, and
  multi-source evidence;
- right column for approval preview, agent/tool execution, and notification /
  execution / audit stream.

Adapt the reference to existing repo constraints:

- preserve local-only display interactions: incident filters, evidence tabs,
  tool detail expansion, and approval status transitions;
- keep real approve/reject/execute calls out of the foundation unless routed
  through Gateway Approval Service and RBAC;
- keep cards at 8px radius or less unless existing CSS requires otherwise;
- avoid decorative gradients/orbs from the reference if they make the UI read as
  a showcase instead of an operations workbench;
- keep implementation in the existing vanilla static frontend.

## Approval Boundary

Approval UI is not decoration. It must communicate that high-risk commands are
blocked until a human with the right scope authorizes them. The foundation should
show:

- pending approval request state;
- required role/scope and blocked reason;
- action proposal risk, resource scope, and rollback plan summary;
- disabled or simulated-only decision controls when not Gateway-backed;
- audit entries that distinguish requested, approved/rejected, and execution
  grant states.

Default state: normal on-call user with `can_approve=false`, showing the
high-risk action blocked by policy and awaiting Incident Commander/service owner
approval. Alternate state: approver user with `can_approve=true`, showing the
same request with enabled decision affordances for UI review only.

Feishu remains notification-only. Browser code must not call Feishu approval
APIs or treat external chat buttons as authoritative approval state.

## Agent Transparency

The Console must show enough structured process data that diagnosis is not a
black box. Show tool-use facts and evidence, not private model reasoning.

Display:

- ordered Agent/tool steps with status and timestamps;
- tool name, source system, query/input summary, duration, and request/evidence
  refs;
- observation summary and result status;
- skipped, missing, partial, and failed evidence reasons;
- diagnosis summary, confidence, root-cause statement, and action proposal link;
- cost/token/latency metadata when Gateway provides it.

Do not display full model chain-of-thought. Prompt snapshots and deep trace
debugging belong to the diagnosis trace/Langfuse path from ADR-0005; this
foundation should only provide a compact Console-native process view and must
not iframe third-party observability tools.

## Layout

- Left sidebar: product name and primary navigation.
- Top bar: environment/cluster, user/role summary, and time window.
- Main content: dense page header, summary metrics, primary work area.
- Tables/lists for scan-heavy data.
- Cards only for repeated items or contained tools; avoid cards inside cards.
- Reserve compact areas for diagnostic cost/usage and Grafana/fallback metadata;
  show `unavailable` or `planned` states rather than fake charts when data is
  absent.
- Panel failures are local: Grafana, cost, approval, or evidence errors must not
  hide the incident summary, diagnosis, or timeline.

## Visual System

Use a restrained multi-hue palette:

- neutral base for chrome and surfaces;
- red for critical/failure;
- amber for partial/warning/approval required;
- green for healthy/succeeded;
- blue/teal for informational agent/tool state;
- avoid a purple/blue gradient-dominant look.

Components:

- status chips with fixed dimensions where practical;
- severity swatches;
- timeline rows with source icons or compact labels;
- evidence/tool rows grouped by source;
- cost/usage summary rows with unavailable/partial states;
- Grafana/fallback placeholders driven only by Gateway-provided metadata;
- readonly action buttons with clear disabled/permission states;
- compact loading, empty, error, unauthorized, unavailable, partial, and
  stale/conflict states.

## Implementation Notes

- Stay vanilla HTML/CSS/JS.
- Reuse fixture-driven render functions where possible.
- Split shared shell styling now:
  - shared CSS for layout, navigation, top bar, panels, tokens, buttons, and
    responsive behavior;
  - page-specific CSS only for unique Overview or diagnosis-detail content;
  - shared JS only for generic local interactions if it stays small.
- Keep text sizes stable; do not scale font size with viewport width.
- Mobile should collapse sidebar to top navigation or compact rail.

## Future Integration

After this design foundation, child tasks can wire:

- Gateway login/session;
- real incident list;
- diagnosis process API;
- approval center as a full workflow;
- notification center;
- constrained Agent follow-up controls.
