# Plan AIOps Console Next rebuild

## Problem Statement

The current AIOps Console is not the target operations control plane. It still
behaves like a thin demo-oriented frontend around incidents, approvals,
notifications, and diagnosis output. Operators need a real Console that can
preserve routes, enforce Gateway-owned authorization, support human-started
agent runs, show live evidence and execution timelines, route production
mutations through policy and approval, and leave an auditable responsibility
chain.

The current codebase already has important platform pieces: Gateway/control
plane, internal approval service, RBAC, audit, notification center, diagnosis
writeback, Connector routing, and Vite/React Console shell. The rebuild must
reuse those boundaries instead of creating a parallel frontend/backend shape.

## Solution

Rebuild the Console as a Gateway-only operations control plane for incidents,
agent runs, evidence, actions, approvals, settings, users, policies, reports,
notifications, and audit responsibility chains.

The first version remains a single-organization internal control plane using
SQLite and one primary Gateway instance. The browser talks only to the Gateway
API. Production serves frontend assets from the Gateway Pod on the same origin
as the API. Agents may propose actions, but Gateway owns policy, grants,
execution, final authorization, and audit.

Implementation should be split into independently verifiable vertical slices.
The parent scope owns the common product contract and final integration smoke.
Child tasks own deliverables such as routing/auth, users/RBAC, evidence,
agent-run streaming, action/grant/approval execution, incident workbench,
audit, settings/policy, reports, and notifications.

## User Stories

1. As an operator, I want to log in and return to the route I originally requested, so that urgent incident links do not lose context.
2. As an unauthenticated user, I want protected routes to send me to login with a return target, so that I can resume the original task after authentication.
3. As a viewer, I want read-only access to incidents and evidence in my scope, so that I can understand operational status without mutation privileges.
4. As an operator, I want to start investigations inside my scope, so that I can ask the platform to gather evidence.
5. As an operator, I want to start a human-initiated agent run, so that I can investigate a problem before an alert exists.
6. As an operator, I want to request an action in chat, so that natural operational requests can become structured Gateway actions.
7. As an approver, I want to approve or reject scoped action requests, so that production changes require accountable human authorization.
8. As an auditor, I want read-only visibility into audit, policy, users, and configuration, so that I can review responsibility without production privileges.
9. As an admin, I want to manage local users, roles, and scopes, so that the Console remains usable without LDAP.
10. As an admin, I want LDAP users to receive local role and scope mappings, so that enterprise identity can coexist with Console authorization.
11. As an admin, I want disabled users to remain in history instead of being deleted, so that audit records keep identity context.
12. As an admin, I want the last admin to be protected from disablement or self-demotion, so that the system cannot lock itself out.
13. As an admin, I want every permission change audited, so that role and scope changes are accountable.
14. As any signed-in user, I want routes to be real URLs, so that refresh and shared internal links preserve the current resource.
15. As any signed-in user, I want unauthorized pages to show a 403 state, so that access denial is explicit without leaking forbidden data.
16. As any signed-in user, I want unknown routes to show a 404 state, so that route mistakes are clear.
17. As an operator, I want incident detail to show summary, status, impact, owner, risk, evidence, chat, recommended actions, approvals, and responsibility context, so that I can work from one screen.
18. As an operator, I want evidence to be first-class across metrics, logs, traces, Kubernetes state, topology, changes, and tool output, so that I do not jump between observability systems.
19. As an operator, I want OpenObserve-backed evidence queries through Gateway, so that tokens and scope checks stay server-side.
20. As a viewer, I want evidence without required scope fields to fail closed, so that unclassified data is not exposed.
21. As an operator, I want failed evidence sources to degrade locally, so that one backend failure does not blank an incident page.
22. As an operator, I want live agent timeline events, so that I can see phases, tool calls, evidence, risk decisions, approvals, and execution progress.
23. As an operator, I want page refresh to resume an agent run timeline, so that I do not lose live context.
24. As an operator, I want SSE replay with persisted events, so that transient disconnects do not lose timeline state.
25. As an operator, I want side conversations marked as `/btw`, so that exploratory questions do not mutate the main run.
26. As an operator, I want to explicitly promote `/btw` findings, so that side discussion only affects the mainline by choice.
27. As an operator, I want ambiguous mutation targets clarified before approval creation, so that the system does not guess production targets.
28. As an approver, I want approval detail to show frozen action, target, risk, evidence, preflight, rollback plan, expected impact, requester, agent, and execution progress, so that I can make an informed decision.
29. As an approver, I want approve and reject actions to require traceable remarks according to policy, so that decisions carry operational context.
30. As an approver, I want self-approval to be allowed only by policy and clearly marked, so that responsibility remains visible.
31. As an approver, I want a pending approval to expire, so that stale production authorization cannot linger indefinitely.
32. As an admin, I want no-approver cases to become blocked and notify admins, so that approval dead ends are visible.
33. As an operator, I want approved actions to execute automatically through Gateway, so that approval grants exactly one frozen action execution.
34. As an operator, I want execution to run preflight, mutation, and post-check, so that unsafe or failed operations stop at the correct stage.
35. As an operator, I want post-check failure to mark rollback required, so that follow-up responsibility is explicit.
36. As an auditor, I want grants bound to action hashes and consumed once, so that approval cannot be reused for a different operation.
37. As an operator, I want mutation locks by target, so that conflicting production changes are serialized.
38. As an admin, I want global and per-cluster concurrency limits, so that agent and mutation load stays controlled.
39. As an auditor, I want responsibility-chain audit to answer what the agent requested, why, who approved, what Gateway executed, and what happened afterward, so that accountability is reviewable.
40. As an auditor, I want deleted conversation tombstones preserved, so that chat deletion does not erase responsibility links.
41. As an operator, I want conversation archive and delete controls for chat-layer content, so that working history can be managed without deleting immutable records.
42. As an operator, I want auto-generated and editable conversation tags, so that runs are searchable by service, cluster, namespace, team, risk, status, source, and evidence domain.
43. As any signed-in user, I want global search across incidents, conversations, runs, approvals, audit chains, users, clusters, namespaces, services, and teams, so that I can find operational objects quickly.
44. As an admin, I want settings sections for cluster/environment, approval policy, action allowlist, notifications, security/session, and feature flags, so that operational behavior can be managed from Console.
45. As an admin, I want settings changes to show a diff before saving, so that risky changes are reviewed before commit.
46. As an admin, I want every settings save to create a version, so that configuration history and rollback are possible.
47. As an admin, I want secrets and internal service URLs hidden from settings, so that Console does not become a secret editor.
48. As an admin, I want policy state and recent policy hits visible, so that authorization behavior can be explained.
49. As an operator, I want notifications in the Console and Feishu/external channels, so that urgent state changes reach humans.
50. As an auditor, I want notification delivery records linked to audit chains, so that notification failures are accountable.
51. As an incident reviewer, I want HTML-first post-incident reports with versions, so that reports are reviewable, printable, and immutable after publish.
52. As an incident reviewer, I want reports to preserve unknowns instead of inventing evidence, so that postmortems remain trustworthy.
53. As a mobile approver, I want login, approval detail, evidence summary, remarks, approve/reject, and execution status on mobile, so that urgent approvals work away from desktop.
54. As a keyboard user, I want labels, focus states, tab order, accessible tabs, and non-color-only risk/status indicators, so that key flows are usable without a mouse.
55. As an operator, I want every page and panel to support loading, empty, partial, error, unauthorized, and stale/conflict states, so that failures are understandable and recoverable.
56. As a deployer, I want the production Console served by Gateway from content-hashed frontend assets, so that routes and API share one origin without a Node/Vite server.
57. As a deployer, I want a rollback path from Gateway-served Console to the previous deployment shape, so that packaging changes can be reverted safely.
58. As a developer, I want child tasks split by user-visible vertical value, so that each agent session can implement and verify one coherent slice.

## Implementation Decisions

- The browser must call only the Gateway/control-plane API. It must not call OpenObserve, Connector, Kubernetes, MCP services, Feishu, host execution backends, or other internal services directly.
- Gateway remains the owner of policy, grants, execution, authorization, scope filtering, audit, evidence redaction, and approval state.
- Production serves the Console from the Gateway Pod on the same origin as the API. Development may continue to use Vite.
- The current independent Console Web deployment is a migration source, not the target production deployment. The plan must include packaging, static asset fallback, route fallback, and rollback behavior.
- The Console uses real routes. In-memory active-tab navigation is not acceptable for primary pages or resource detail pages.
- Production sessions use HttpOnly same-origin cookies. Browser-readable bearer tokens may remain only for development, tests, or service compatibility.
- Mutating routes use CSRF protection. The frontend sends the Gateway-provided CSRF token on approval, users, settings, policy, action, and other write requests.
- The target platform roles are `viewer`, `operator`, `approver`, `auditor`, and `admin`. Existing role names need an explicit migration and compatibility mapping.
- Authorization scope uses `cluster`, `namespace`, `service`, and `team`. Missing resource scope fails closed for non-admin users.
- The first version is single-organization. Tenant IDs, organization switching, and billing are out of scope.
- SQLite remains the first storage backend. It stores users, scopes, conversations, agent runs, SSE events, approvals, grants, audit, settings versions, and durable refs. WAL and write serialization are required where needed.
- Large raw evidence is not stored directly in SQLite. The system stores summaries and references.
- Schema design must leave room for future PostgreSQL migration, but PostgreSQL is not part of the first version.
- Local users and LDAP users are both supported. Console can manage local passwords, but not LDAP passwords.
- `/users` is required with list, detail, create, disable, reset local password, role assignment, scope assignment, source display, recent login, and recent permission-change audit.
- `/settings` is required with immediate versioned saves, diff before save, critical-change confirmation, audited changes, and rollback to the previous version.
- `/policies` is required for cluster/environment-aware policy state, action allowlists, and recent policy hits. First implementation can be read-heavy.
- OpenObserve is the first-class logs, metrics, and traces backend for the next version. Existing Prometheus, Loki, and Topology MCP paths remain compatibility or fallback paths during migration.
- Evidence query permissions are scope-filtered, audited, time-limited, row-limited, timeout-limited, and redacted. Agents use allowlisted tool templates.
- Agent workbench supports incident-triggered and human-started runs. The interaction model is chat plus timeline.
- Mainline conversation state can mutate runs, evidence, actions, approvals, timeline, and audit. `/btw` side conversations do not mutate mainline until explicitly promoted.
- Gateway converts recognized chat intents into structured Actions. Mutation targets must not be guessed.
- All changes use Action -> policy -> grant -> execution -> audit, regardless of backend.
- First implementation supports Kubernetes read, Kubernetes allowlisted mutation, and notification.
- Approval grants one execution of one frozen action. Policy auto-allowed mutations use policy grants. Human-approved actions use human approval grants.
- Approval-passed means Gateway executes the frozen action automatically, subject to grant, lock, preflight, mutation, and post-check.
- Execution order is preflight -> mutation -> post-check. Preflight failure blocks mutation. Post-check failure marks rollback required.
- Mutation locks are keyed at least by backend, cluster, namespace, and target resource/action target.
- Each incident has at most one active mainline run by default. Side threads are allowed.
- SSE is used for realtime run output. Events are persisted, support Last-Event-ID, are authorized per user, and are redacted.
- Audit is responsibility-chain first, raw-log second. Immutable responsibility records must not be deleted.
- Conversation chat-layer content can be physically deleted, but responsibility-chain records stay immutable and deletion tombstones are kept.
- Reports are HTML-first, versioned, draft before publish, immutable after publish, printable, exportable as HTML, and shared only by authenticated internal links.
- Notifications include Console notification center, Feishu/external notifications, and page-level realtime toast/SSE events. Feishu remains notification-only and never owns approval state.
- Mobile support focuses on urgent approval and incident awareness, not full desktop investigation or settings management.
- Basic accessibility is an acceptance criterion, not polish.
- Visual design is dense, evidence-first, and operations-console oriented. No marketing homepage or decorative overview placeholder.

## Testing Decisions

- The highest-value test seam is the Gateway API plus Console route behavior: tests should validate externally visible authorization, routing, persistence, event replay, approval execution, audit, and UI state rather than internal helper functions.
- Existing Gateway HTTP tests are prior art for auth, RBAC, incident scope filtering, approval service, approval execution, notification center, audit, Connector routing, and degraded backend behavior.
- Existing Console contract tests are prior art for Gateway-only frontend calls, build behavior, route/asset assumptions, responsive layout, and forbidden direct service calls.
- New route tests should verify login `next`, refresh preservation, 403, 404, permission-filtered navigation, and the absence of in-memory primary route state.
- New session tests should verify HttpOnly cookie login, logout, 401 expiry handling, CSRF token issuance, and CSRF enforcement on mutating routes.
- New RBAC tests should verify five-role permissions, legacy role migration, four-dimensional scopes, missing-scope fail-closed behavior, and last-admin protections.
- New users tests should verify local user lifecycle, LDAP user restrictions, role/scope updates, disabled users, recent login display, and permission-change audit.
- New settings tests should verify diff before save, version creation, rollback, critical confirmation, secret hiding, reload-required state, and audit records.
- New evidence tests should verify OpenObserve query routing through Gateway, scope mapping, row/time/timeout limits, redaction, advanced-query permissions, and fallback degradation.
- New SSE tests should verify snapshot-before-stream, persisted events, Last-Event-ID replay, redacted payloads, and per-user event authorization.
- New action/grant/approval tests should verify frozen action hash, policy classification, one-time grant consumption, idempotency replay, self-approval audit, no-approver blocked state, and automatic execution after approval.
- New lock/concurrency tests should verify one active mainline run by default, side thread allowance, global agent limits, per-cluster mutation limits, target locks, timeout, and recovery.
- New audit tests should verify responsibility-chain fields, immutable records, raw-log refs, deletion tombstones, and notification delivery links.
- New report tests should verify draft generation, publish immutability, versioning, authenticated access, printable/exportable HTML, and no invented evidence.
- New mobile/accessibility tests should verify approval flow on mobile, labels, keyboard order, visible focus, modal focus management, color-independent status, readable contrast, and keyboard-accessible evidence tabs.
- End-to-end smoke should cover login -> open incident -> start agent run -> see live evidence/tool timeline -> request service restart -> approve -> Gateway executes mock -> audit shows responsibility chain.

## Acceptance Criteria

- [x] The parent PRD is accepted as the source requirement set for the Console Next rebuild.
- [x] Child tasks are created for independently verifiable vertical slices rather than frontend/backend layers.
- [x] Each child task has its own PRD and, for complex slices, design and implementation plans before implementation starts.
- [x] The first batch can deliver the core loop: login -> request investigation/action -> live evidence/tool timeline -> approval when needed -> Gateway auto-execution -> audit trail.
- [x] The final integration smoke passes through Gateway-only browser/API boundaries.
- [x] No child task requires the browser to call internal services directly.
- [x] No child task implements high-risk mutation outside Gateway policy, grant, approval, lock, execution, and audit.

## Out of Scope

- Multi-tenant organizations, tenant switching, tenant billing, and tenant-isolated settings.
- OAuth/OIDC until a concrete enterprise identity source is selected.
- Multi-replica Gateway production support before PostgreSQL migration.
- PostgreSQL migration itself.
- Public unauthenticated report sharing.
- Complex personal notification preferences in the first version.
- Automatic model training from feedback in the first version.
- Comfortable mobile settings management, user management, or full evidence analysis.
- Production host execution backend in the first implementation; future host execution must still use Action -> policy -> grant -> execution -> audit.
- A separate production Console Web Pod in the target first version.
- Marketing homepage, overview placeholder, or decorative frontend rebuild disconnected from operational workflows.

## Further Notes

Recommended child-task map:

1. Routing, Gateway-served assets, cookie session, CSRF, and route guard shell.
2. Five-role RBAC, scope migration, and user management.
3. Settings versioning, policy visibility, and action allowlist management.
4. OpenObserve evidence query, evidence model, and evidence panels.
5. Conversations, agent runs, `/btw`, and persisted SSE timeline.
6. Action, grant, approval, automatic execution, locks, and idempotency.
7. Incident workbench controls, takeover, resolve, reopen, and run concurrency.
8. Responsibility-chain audit and deletion tombstones.
9. HTML incident reports and feedback capture.
10. Console notification center and external notification delivery records.
11. Global search, mobile approval flow, accessibility, and final integration smoke.

Proposed testing seam for review: keep most tests at the Gateway HTTP API and
Console route/user-flow level, with small domain tests only where pure state
machines or hashes need direct coverage. This matches the current codebase
better than testing many low-level helpers.
