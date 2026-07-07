# Console Next OpenObserve evidence query and panels

## Parent

Plan AIOps Console Next rebuild.

## What to build

Make evidence a first-class Gateway-owned model backed by OpenObserve for logs,
metrics, and traces, with compatibility fallback for current Prometheus, Loki,
Kubernetes, and Topology paths. Incident, approval, and run views can show
evidence summaries, refs, failures, redaction state, and partial results.

The completed slice should be demoable as: scoped operator queries evidence for
a service/time range, Gateway applies scope, limits, redaction, and audit, and
Console renders metrics/logs/traces/Kubernetes/topology panels with partial
failure states.

## User Stories Covered

- Parent stories 17, 18, 19, 20, 21, 28, 52, and 55.

## Requirements

- Browser never receives OpenObserve tokens and never calls OpenObserve
  directly.
- Gateway queries OpenObserve and applies user scope.
- Evidence scope maps cluster, namespace, service, team, and environment fields.
- Missing scope fields fail closed for non-admin users.
- Evidence query supports metrics, logs, traces, Kubernetes state/events,
  topology/dependency, changes, and tool output where available.
- Existing Prometheus, Loki, and Topology MCP paths remain compatibility or
  fallback during migration.
- Evidence queries have time range, row count, and timeout limits.
- Viewers, operators, and approvers use templated queries by default.
- Admin and auditor may use advanced queries when allowed, still scope-limited
  and audited.
- Agents use allowlisted tool templates only.
- Gateway redaction is mandatory even if backend redaction exists.
- Secrets, tokens, passwords, authorization headers, and Kubernetes Secret
  contents are never shown.
- Evidence panels render loading, empty, partial, error, unauthorized, and stale
  states independently.

## Acceptance Criteria

- [ ] Evidence query API supports scoped manual and agent-triggered evidence
  lookup.
- [ ] OpenObserve-backed metrics, logs, and traces return summarized,
  redacted, auditable evidence refs.
- [ ] Compatibility fallback can render current Prometheus, Loki, Kubernetes,
  and Topology evidence where available.
- [ ] Missing scope fields are hidden from non-admin users.
- [ ] Query limits and timeouts are enforced server-side.
- [ ] Evidence failures do not blank the whole page.
- [ ] Evidence panels are reusable in incident, approval, and agent-run views.
- [ ] Audit records include requester, scope, query type/template, result, and
  request id.
- [ ] Tests cover scope mapping, fail-closed behavior, redaction, limits,
  degraded backends, and panel states.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.
- Console Next five-role RBAC and user management.

## Further Notes

Keep raw evidence out of SQLite. Store summaries and refs unless a later task
explicitly changes retention/storage rules.
