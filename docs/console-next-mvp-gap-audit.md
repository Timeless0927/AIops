# Console Next MVP Gap Audit

Date: 2026-07-09

Status: MVP release closeout for accepted GitHub issues #53-#68.

Source of truth: `docs/aiops-console-next-plan.md`, especially the MVP first
batch, plus the already accepted #53-#68 release slices.

Scope guard: this closeout adds no business functionality.

## Verdict

Console Next is closed out for the accepted MVP release scope. The verified
release loop is:

```text
login -> incident workbench -> runbook-driven agent run with SSE
-> scoped evidence process graph -> chat/action request
-> policy/grant/approval -> Gateway execution -> responsibility-chain audit
```

The 2026-07-08 blockers are closed by #53-#68:

- Gateway production entrypoint and legacy route cleanup: #53, #68.
- RBAC write boundaries, users, and operator action request path: #54, #60.
- Cluster/OpenObserve scoped evidence and incident/run presentation: #55, #56,
  #58.
- Runbook Orchestrator and `GET /api/agent-runs/:runId/events` SSE contract:
  #57.
- Policy, grants, approvals, execution, and audit chain: #59, #60, #61.
- Reports, notifications, mobile approval, accessibility smoke, runbook toggles,
  inline KB review, and global search: #62-#67.

## Final Validation

Backend related tests:

```bash
pytest -q tests/test_gateway_console_next_session.py tests/test_gateway_users.py tests/test_gateway_identity_rbac.py tests/test_gateway_clusters.py tests/test_gateway_evidence_openobserve.py tests/test_gateway_agent_runs_sse.py tests/test_gateway_runbooks.py tests/test_gateway_incident_workbench.py tests/test_gateway_actions_approvals_execution.py tests/test_gateway_approval_service.py tests/test_gateway_settings_policy.py tests/test_gateway_audit_chains.py tests/test_gateway_reports_feedback.py tests/test_gateway_console_notifications.py tests/test_gateway_search_mobile_smoke.py tests/test_aiops_console_web.py
```

Result: 73 passed, 44 warnings in 103.56s.

Frontend build/typecheck:

```bash
cd apps/aiops_console_web && npm run build
```

Result: `tsc --noEmit && vite build` passed; Vite built production assets in
1.63s.

## Issue Closeout

Checked with GitHub on 2026-07-09. All #53-#68 issues are closed; there are no
open closeout reasons to list.

| Issue | State | Closeout |
| --- | --- | --- |
| #53 Console Next: Gateway-served shell, login, and route recovery | Closed | Complete. |
| #54 Console Next: Users, roles, and scoped RBAC enforcement | Closed | Complete. |
| #55 Console Next: Cluster registry and OpenObserve health | Closed | Complete. |
| #56 Console Next: Scoped evidence query with process-node presentation | Closed | Complete. |
| #57 Console Next: Runbook Orchestrator and Agent Run SSE | Closed | Complete. |
| #58 Console Next: Incident Workbench main experience | Closed | Complete. |
| #59 Console Next: Policy table, action allowlist, and settings audit | Closed | Complete. |
| #60 Console Next: Chat action to approval to automatic execution | Closed | Complete. |
| #61 Console Next: Responsibility-chain audit across run, approval, and execution | Closed | Complete. |
| #62 Console Next: HTML incident reports with KB candidate generation | Closed | Complete. |
| #63 Console Next: scoped notifications and direct approval links | Closed | Complete. |
| #64 Console Next: mobile approval flow and accessibility smoke | Closed | Complete. |
| #65 Runbook management MVP: view skeletons and toggle availability | Closed | Complete. |
| #66 Inline KB candidate review in incident/report workflows | Closed | Complete. |
| #67 Gateway-backed global search | Closed | Complete. |
| #68 Remove legacy Console paths and navigation entry points | Closed | Complete. |

## Completed MVP Scope

| Plan item | Status |
| --- | --- |
| Login, real routes, session recovery, 403/404, RBAC, users | Complete via #53, #54. |
| Cluster management, OpenObserve health, scoped evidence query | Complete via #55, #56. |
| Incident Workbench, evidence process graph, Agent Run detail | Complete via #56, #57, #58. |
| Runbook Orchestrator, conversations, human-started tasks, SSE timeline | Complete via #57, #60, #65. |
| Action, grant, approval, automatic Gateway execution | Complete via #59, #60, #64. |
| Responsibility-chain audit | Complete via #61. |
| Accepted MVP extensions: reports, notifications, inline KB review, search | Complete via #62, #63, #66, #67. |
| Legacy Console route and production entrypoint cleanup | Complete via #68. |

## Still Deferred

- PostgreSQL and multi-replica Gateway; MVP remains single-Gateway SQLite.
- OAuth/OIDC; local users and LDAP remain the supported identity sources.
- Host execution and broad custom backends; MVP keeps Gateway-owned allowlisted
  actions.
- Audited raw-evidence debugging path; default evidence remains summarized and
  redacted.
- Full Knowledge Base management page; MVP uses inline candidate review.
- Drag/drop or full runbook authoring; MVP supports view plus enable/disable.
- Complex mobile investigation, settings, and user-management workflows.
- Automatic model training from feedback.
- Multi-tenant organization switching, tenant billing, and `tenant_id`.

## Explicitly Not In This MVP

- Separate production Console Web Pod or production Node/Vite server.
- Browser calls directly to OpenObserve, Connector, K8s, MCP services, Feishu,
  or host execution backends.
- Standalone `/evidence` main navigation or raw-JSON-first evidence explorer.
- Legacy production `/approval-center` or `/notification-center` route paths.
- Agent-executed high-risk production mutation without Gateway approval.
- Self-service registration.
- Public unauthenticated incident report sharing.

## Next Batch Suggestions

1. PostgreSQL/HA migration when multi-replica Gateway becomes real.
2. Enterprise OIDC only after a concrete identity provider is selected.
3. Richer evidence drilldown with an audited raw-evidence escape hatch.
4. Runbook authoring beyond enable/disable.
5. Additional execution backends such as deployment, feature flag, and host.
