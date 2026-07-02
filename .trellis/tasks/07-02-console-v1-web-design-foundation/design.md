# Design: Console V1 web foundation

## Product Direction

Console V1 should feel like an operational control room: dense, calm, readable,
and trustworthy. It should prioritize fast triage and auditability over visual
decoration.

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

First static screens:

- `console-shell.html`: overview/list composition and navigation baseline.
- `incident-detail.html`: upgraded diagnosis process detail using the shared
  shell style.
- Approval preview section in shell or a small `approval-center.html` if the
  layout would become crowded.

## Layout

- Left sidebar: product name, environment, navigation.
- Top bar: cluster/profile, time window, user/role summary.
- Main content: dense page header, summary metrics, primary work area.
- Tables/lists for scan-heavy data.
- Cards only for repeated items or contained tools; avoid cards inside cards.

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
- readonly action buttons with clear disabled/permission states;
- compact empty/error states.

## Implementation Notes

- Stay vanilla HTML/CSS/JS.
- Reuse fixture-driven render functions where possible.
- Prefer one shared stylesheet for shell primitives and page-specific small
  additions.
- Keep text sizes stable; do not scale font size with viewport width.
- Mobile should collapse sidebar to top navigation or compact rail.

## Future Integration

After this design foundation, child tasks can wire:

- Gateway login/session;
- real incident list;
- diagnosis process API;
- approval center;
- notification center;
- constrained Agent follow-up controls.
