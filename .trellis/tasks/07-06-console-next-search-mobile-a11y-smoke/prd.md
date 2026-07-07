# Console Next search, mobile approval, accessibility, and final smoke

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver cross-cutting Console completion work: permission-filtered global
search, mobile urgent approval flow, accessibility acceptance coverage, and the
final end-to-end smoke through the core loop.

The completed slice should be demoable as: mobile approver opens an approval
notification link, logs in, reviews evidence summary, adds remark, approves,
and sees execution status. Desktop user can search operational objects. Final
smoke proves the full Gateway-only loop.

## User Stories Covered

- Parent stories 1, 14, 15, 16, 43, 53, 54, 55, and 58.

## Requirements

- Global search covers incidents, conversations, agent runs, approvals, audit
  chains, users, clusters, namespaces, services, and teams.
- Search results are permission-filtered.
- Search results link to real routes.
- Search uses Gateway API, not local frontend joins.
- Sensitive object access still follows normal audit/query rules.
- Mobile support includes login, incident summary, approval detail, evidence
  summary, approve/reject, approval remarks, execution status, and opening
  notification links.
- Mobile does not need comfortable settings management, user management, or full
  evidence analysis.
- Accessibility basics are required: labels, keyboard tab order, visible focus,
  color-independent risk/status, specific dangerous labels, modal focus
  management, clear loading/error/empty text, keyboard-accessible evidence tabs,
  readable font/contrast, and non-disruptive animation.
- Final smoke covers login -> open incident -> start agent run -> see live
  evidence/tool timeline -> request service restart -> approve -> Gateway
  executes mock -> audit shows responsibility chain.

## Acceptance Criteria

- [ ] Global search returns permission-filtered objects and links to real routes.
- [ ] Search does not join hidden frontend-only data to bypass Gateway access.
- [ ] Mobile approval flow works from notification/deep link through execution
  status.
- [ ] Mobile approval includes evidence summary and remarks.
- [ ] Labels, keyboard navigation, visible focus, modal focus, contrast, and
  color-independent status are covered for key flows.
- [ ] Loading, error, empty, unauthorized, partial, and stale/conflict states are
  reviewed across core pages.
- [ ] Final Gateway-only end-to-end smoke passes.
- [ ] Build/typecheck and focused frontend/backend tests pass.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.
- Console Next five-role RBAC and user management.
- Console Next settings versions and policy management.
- Console Next OpenObserve evidence query and panels.
- Console Next conversations, agent runs, and SSE timeline.
- Console Next action grants, approvals, execution, and locks.
- Console Next incident workbench and run controls.
- Console Next responsibility-chain audit and tombstones.
- Console Next HTML reports and human feedback.
- Console Next notification center delivery records.

## Further Notes

This is intentionally last. It validates that the earlier slices compose into
the product loop, and catches design/accessibility gaps before closeout.
