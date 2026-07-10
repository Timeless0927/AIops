Type: grilling
Status: resolved

## Question

What is the minimal first-version Console route and navigation scope for the Incident Workbench product, and which old pages must be removed, hidden, or folded into the workbench?

## Context

The old Console exposes many first-class pages: incidents, Agent Runs, Runbooks, approvals, audit, policies, clusters, users, settings, search, notifications, reports, KB candidates, and more. The user has rejected this as feature sprawl with weak product meaning.

Resolve a route list that is small enough to implement and test, while still supporting the first-version event investigation loop.

## Answer

First-version Console route scope:

- `/login`
- `/incidents`
- `/incidents/:incidentId`
- `/incidents/:incidentId/report`

Primary navigation contains only the Incident Workbench path. The product opens around incidents, not around a dashboard, run list, admin console, or search page.

Folded into incident detail:

- Agent Runs become an event-scoped investigation thread. The backend may keep run records, but users do not navigate to a standalone Agent Runs product.
- Approvals become "待确认动作" or "需要确认" inside the incident right column. There is no standalone approval center in the first version.
- Audit becomes a responsibility/audit summary inside incident detail. There is no standalone audit page in the first version.
- Resource state appears in incident context: cluster, namespace, service, team, Connector status, permission/policy blockers, and unbound-resource warnings.

Kept as a lightweight child page:

- `/incidents/:incidentId/report` remains as incident report preview/export. It shows known facts, timeline, evidence summary, conclusion, human notes, and follow-up items. Unknowns stay explicit. No report versioning, publish flow, or KB candidate generation in the first version.

Removed from first-version top-level scope:

- `/agent-runs`
- `/agent-runs/new`
- `/agent-runs/:runId`
- `/runbooks`
- `/approvals`
- `/approvals/:approvalId`
- `/audit`
- `/audit/:chainId`
- `/policies`
- `/clusters`
- `/users`
- `/users/:userId`
- `/settings`
- `/search`

No standalone admin/management navigation is included in the first version. Cluster/user/resource management should wait for the resource binding model ticket; until then the workbench only displays the resource state needed to understand the incident.
