# Console Next search/mobile/accessibility/smoke design

## Scope

This final slice validates cross-cutting product completeness: global search,
mobile urgent approval, accessibility, and the final Gateway-only smoke.

## Confirmed decisions

- Search uses a Gateway API.
- Search does not perform frontend local joins over hidden data.
- First search implementation is cross-object unified search across allowed
  incidents, conversations, agent runs, approvals, audit chains, users, clusters,
  namespaces, services, and teams.
- Mobile first version guarantees approval flow only.
- Mobile approval includes login, approval detail, evidence summary, remarks,
  approve/reject, and execution status.
- Accessibility is a final smoke gate rather than a separate broad redesign.
- Final smoke is fixed:
  login -> open incident -> start agent run -> see live evidence/tool timeline
  -> request service restart -> approve -> Gateway executes mock -> audit shows
  responsibility chain.

## Deferred

- Full mobile settings.
- Full mobile user management.
- Full mobile evidence analysis.
- Standalone accessibility rewrite outside the core flows.
