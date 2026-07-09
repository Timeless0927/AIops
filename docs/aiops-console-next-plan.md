# AIOps Console Next Product and Architecture Decisions

Date: 2026-07-06

Status: Accepted current product source for Console Next

## Purpose

This document captures the agreed direction for the next AIOps Console. The
current frontend is not the target. The next version must be a real operations
control plane for humans, agents, approvals, evidence, and audit responsibility.

The core product loop is:

```text
login -> investigate or chat -> agent run -> live evidence/tool timeline
-> action proposal -> policy/grant -> human approval when required
-> Gateway execution -> audit responsibility chain
```

The default authenticated experience is the Incident Workbench. The product is
not a generic dashboard, approval inbox, raw evidence explorer, or marketing
home page. After login, the default route opens the latest unresolved incident
detail that the user is allowed to see.

## Non-Negotiable Boundaries

- Browser talks only to the Gateway/control-plane API.
- Browser must not call OpenObserve, Connector, K8s, MCP services, Feishu, or
  future host execution backends directly.
- Agent may propose actions; Gateway owns policy, grant, execution, audit, and
  final authorization.
- High-risk production changes must not be executable by an agent without a
  Gateway-controlled approval path.
- Frontend permission checks are only UX. Backend authorization remains final.
- Refreshing a page must preserve the route and current resource, not reset to a
  default tab.

## Roles

The platform has five roles:

| Role | Purpose |
| --- | --- |
| `viewer` | Read-only access to incidents, evidence, agent runs, and reports inside scope. |
| `operator` | Can start investigations and request/propose operations inside scope. |
| `approver` | Can approve scoped actions. Self-approval is allowed by policy and must be audited. |
| `auditor` | Read-only audit, policy, user, and configuration visibility. No production actions. |
| `admin` | User/scope/settings management and audit access. Admin does not bypass action policy. |

Runtime responsibilities such as `incident_commander` and `service_owner` are
not global platform roles. They are incident or ownership relationships used to
pick approval candidates and display accountability.

Role boundaries:

- `viewer` is read-only and cannot message the Agent, start runs, or request
  actions.
- `operator` can start investigations, chat, and request or propose actions, but
  cannot approve.
- `approver` can approve scoped actions according to policy.
- `admin` can manage users, settings, policy, and clusters, but cannot bypass
  action policy.
- `auditor` is read-only for audit, policy, users, and configuration.

## Identity Sources

The Console supports local users and LDAP users.

Rules:

- Local users keep the system independently usable.
- LDAP users can log in when LDAP is configured.
- `/users` shows each user's source: `local` or `ldap`.
- Admins can create, disable, reset passwords for, and assign roles/scopes to
  local users.
- Admins cannot change LDAP user passwords in Console.
- LDAP users still receive local role/scope mappings.
- Self-service registration is not supported.
- OAuth/OIDC is not in the first version unless a concrete enterprise identity
  source is later selected.

## Scope Model

Authorization scope uses four dimensions:

- `cluster`
- `namespace`
- `service`
- `team`

Rules:

- Non-admin users are scope-limited.
- Missing resource scope fails closed.
- Unclassified evidence is hidden from non-admin users.
- `auditor` may be global read-only or scope-limited by configuration.
- `admin` can manage globally, but actions still go through policy/grant.

## Tenancy

The first version is a single-organization internal control plane.

Supported:

- Multiple clusters.
- Multiple namespaces.
- Multiple teams.
- Multiple services.
- RBAC scope isolation.

Not supported:

- `tenant_id`.
- Organization switching.
- Tenant-level billing.
- Tenant-isolated settings.

## Routes

The Console uses real routes, preferably with `react-router`.

Primary routes:

- `/login`
- `/incidents`
- `/incidents/:incidentId`
- `/incidents/:incidentId/report`
- `/agent-runs`
- `/agent-runs/new`
- `/agent-runs/:runId`
- `/approvals`
- `/approvals/:approvalId`
- `/audit`
- `/audit/:chainId`
- `/clusters`
- `/policies`
- `/users`
- `/users/:userId`
- `/settings`

Route rules:

- Unauthenticated users go to `/login?next=...`.
- Successful login returns to `next`.
- Unauthorized routes show a 403 page.
- Unknown routes show a 404 page.
- Navigation hides pages the user cannot access.
- No route should be implemented as in-memory `activeView` state.
- There is no standalone `/evidence` main navigation in the first version.
  Evidence appears inside incidents, agent runs, approvals, and audit chains.

## Deployment

Production serves the Console from the Gateway Pod.

Shape:

```text
Browser -> Gateway Pod
          -> GET /login, /incidents/:id, /agent-runs/:id returns frontend assets
          -> GET/POST /api/* handles API requests
```

Rules:

- Vite build output is packaged into the Gateway image.
- Production does not expose a Node/Vite server.
- Development may use Vite dev server.
- Gateway serves static assets and API from the same origin.
- Frontend route fallback returns `index.html` for application routes.
- Static assets should be versioned or content-hashed to avoid cache mismatch.
- A separate Console Web Pod is out of scope for the first version.
- Any legacy `aiops-console-web` Deployment, Service, or ingress path should be
  removed or marked legacy during migration. Gateway is the production serving
  boundary.

## Session Security

Production sessions use HttpOnly cookies instead of browser-readable tokens.

Rules:

- Login sets `HttpOnly; Secure; SameSite=Lax` session cookie.
- Frontend does not store or read session tokens.
- Same-origin API requests automatically carry the session cookie.
- Logout clears the cookie.
- Bearer token support may remain for development, tests, or service
  compatibility.
- Mutating routes, including approval/settings/users writes, require CSRF
  protection.
- Gateway provides a CSRF token for the frontend to send as `X-CSRF-Token`.
- Session expiry returns 401; frontend redirects to `/login?next=<current-path>`.

## Users

`/users` is required.

Capabilities:

- List users.
- View user detail.
- Create local users.
- Enable/disable users.
- Reset local user passwords.
- Assign roles: `viewer`, `operator`, `approver`, `auditor`, `admin`.
- Configure scope: `cluster`, `namespace`, `service`, `team`.
- Show login source: local or LDAP.
- Show recent login time.
- Show recent permission-change audit.

Rules:

- Only `admin` can write.
- `auditor` can view read-only.
- Users are disabled, not deleted.
- The last admin cannot be disabled.
- A user cannot remove their own final admin capability.
- LDAP users cannot have local passwords changed in Console.
- All permission changes are audited.

## Storage

The first version continues to use SQLite.

Constraints:

- Primary deployment is a single Gateway instance.
- SQLite stores users, scopes, conversations, agent runs, SSE events, approvals,
  grants, audit, and settings versions.
- SQLite should run with WAL enabled.
- Writes should be serialized where needed to avoid lock contention.
- Large raw evidence is not stored directly in SQLite; store summaries and refs.
- Schema design should leave room for future PostgreSQL migration.
- Multi-replica Gateway support requires a later PostgreSQL migration.

## Settings

`/settings` is required and can modify runtime/system configuration. It is not a
dumping ground for secrets or arbitrary environment variables. Policy is more
important than generic Settings and is edited through policy-specific controls,
not a raw JSON editor. Cluster source-of-truth records live in `/clusters`;
Settings may link to them or configure defaults.

Settings has six sections:

1. Cluster and environment
   - Cluster display name.
   - Environment: `prod`, `staging`, `dev`, `test`.
   - Enable/disable automatic actions.
   - Default namespace scope.
   - Owner team.
   - Notes.

2. Approval policy
   - Rules by `environment + namespace + action_type + risk_level`.
   - Whether approval is required.
   - Whether approval triggers automatic execution.
   - Approval pending timeout.
   - Self-approval policy.
   - Eligible approver roles.

3. Action allowlist
   - Action type.
   - Backend.
   - Command/action template.
   - Allowed environments and scopes.
   - Default risk.
   - Preflight requirement.
   - Post-check requirement.
   - Rollback plan requirement.
   - Enabled/disabled.

4. Notification configuration
   - Service/team to Feishu or external channel mapping.
   - Incident notification switch.
   - Approval notification switch.
   - Execution-result notification switch.
   - Dedup window, retry count, dead-letter behavior.

5. Security and session
   - Session TTL.
   - Login failure lockout threshold.
   - Approval pending timeout.
   - Required approval remarks.
   - Read-only maintenance mode.

6. Feature flags
   - Agent automatic approval request creation.
   - Approval-passed automatic execution.
   - Low-risk mutation auto-execution in non-prod.
   - Experimental agent trace visibility.
   - User-management writes.
   - Detailed policy-hit logs.

Settings rules:

- Only `admin` can write.
- Save is immediate; no draft/publish workflow.
- Every save creates a configuration version.
- Frontend shows a diff before saving.
- Critical changes require confirmation text.
- All changes are audited.
- Rollback to the previous version is supported.
- Secrets, tokens, LDAP bind passwords, internal service URLs, and database paths
  are not editable or displayed.
- If a change needs restart/reload, the UI must show that state.

## Policy and Cluster Risk

Policy is cluster/environment aware.

Default behavior:

| Environment | Read-only | Low-risk mutation | Medium/high mutation |
| --- | --- | --- | --- |
| `prod` | Automatic | Approval required | Approval required |
| `staging` | Automatic | Automatic with policy grant | Approval required |
| `dev` / `test` | Automatic | Automatic with policy grant | Configurable |

Unconfigured clusters are treated as `prod`.

`/policies` is required. It shows policy state, cluster environments, allowlists,
and recent policy hits. First implementation can be read-heavy, but settings must
eventually modify policy.

MVP policy UI is a simple rule table, not a JSON editor. Rules are expressed as
rows over `environment`, optional `cluster`, optional `namespace`,
`action_type`, `risk_level`, approval requirement, auto-execution behavior,
self-approval behavior, and eligible approver roles.

## Cluster Management

`/clusters` is required for minimal multi-cluster management.

Cluster configuration fields:

- Cluster id.
- Display name.
- Environment: `prod`, `staging`, `dev`, `test`.
- Default namespace scope.
- Owner team.
- Automatic actions enabled/disabled.
- OpenObserve query configuration reference.

Runtime state fields:

- Connector status.
- OpenObserve status.
- Scope field mapping health.
- Recent query health.
- Failure summary.
- Last heartbeat/update time.

Ownership rules:

- Admin owns cluster configuration.
- Gateway or Connector reports runtime state.
- An unconfigured cluster is shown as `unconfigured`, treated as `prod`, and
  receives no permissive defaults.
- A configured cluster without a live connector is shown as degraded/offline.
  Mutation is disabled, but historical evidence summaries remain visible.
- Console does not display or edit OpenObserve tokens, internal URLs, secrets,
  or database paths.

## Agent Workbench

The Console must support both incident-triggered and human-started agent runs.
The user-facing model is one AIOps Agent Run with multiple tools and evidence
domains, not a visible set of separate specialist agents in the MVP.

Entry points:

- Alert/incident triggers an agent run.
- Human starts a run from `/agent-runs/new`.
- Human asks inside an incident or existing conversation.
- Chat commands may request actions, such as restarting a service.

The interaction model is chat plus timeline:

- Chat lets users ask questions, start investigations, request actions, and use
  `/btw` side conversations.
- Timeline shows live phases, tool calls, evidence, risk decisions, approvals,
  and execution progress.

Incident detail and Agent Run detail have different jobs:

- Incident detail is the event workbench. It answers: what is happening and what
  do we do now?
- Agent Run detail is one execution record. It answers: what exactly did this
  run do, step by step?
- Incident detail embeds the current run summary and can link to historical
  runs.

First agent implementation is a deterministic Runbook Orchestrator, optionally
with LLM-generated summaries. Gateway creates and manages runs, events,
evidence, actions, approvals, execution, and audit. The first version is not a
fixture-only frontend and not an open-ended autonomous agent.

Runbook skeletons constrain investigation flow and prevent runaway exploration.
Initial skeletons are limited to `service_health`, `k8s_workload`, and
`dependency`. The Agent fills in evidence and summaries inside the selected
skeleton, but cannot freely invent an unbounded flow. MVP Runbook UI is view
plus enable/disable; drag/drop editing is not in the first version.

Mainline vs `/btw`:

- Mainline affects run state, evidence, actions, approvals, timeline, and audit.
- `/btw` is side discussion and does not mutate mainline state.
- A `/btw` result must be explicitly promoted before it affects the mainline.

The system may show:

- Diagnosis plan.
- Hypotheses.
- Why a tool is being called.
- Tool input/output summaries.
- Evidence references.
- Risk classification summary.
- Confidence and unknowns.

The system must not expose:

- Raw chain-of-thought.
- Hidden prompts.
- Secrets.
- Unsanitized large raw logs.

## Human-Started Actions from Chat

Users can type natural requests such as:

```text
重启 prod-a/default 的 checkout 服务
```

Flow:

1. Agent recognizes intent, for example `restart_service`.
2. Agent extracts target fields.
3. Missing or ambiguous mutation targets are clarified with the user.
4. Gateway converts the intent to a structured Action.
5. Gateway verifies requester scope.
6. Gateway applies policy and risk classification.
7. Gateway creates approval or policy grant as required.
8. After approval, Gateway automatically executes the frozen Action.
9. Timeline streams preflight, mutation, post-check, and result.

If the request is complete and unambiguous, the Agent may create the structured
Action and Gateway approval request without an extra user "submit approval"
click. If the target, action type, or risk-relevant fields are ambiguous, the
Agent must clarify before Gateway creates the approval or policy grant.

Responsibility fields:

- `requester`: user who requested the action.
- `agent`: agent that parsed/recommended the action.
- `approver`: human approver when required.
- `executor`: Gateway/system.
- `policy`: policy decision used to allow, block, or require approval.

Mutation targets must not be guessed. If a target is inferred from incident
context, the UI must show the inferred target before creating approval.

## Conversations

Agent interactions are managed as conversations.

`conversation`:

- User-visible container.
- Can be human-created or incident-created.
- Can link to an incident.
- Can contain multiple agent runs.
- Has title, tags, status, creator, scope, and timestamps.

`agent_run`:

- One agent execution.
- Belongs to a conversation.
- Has timeline, tool calls, evidence, actions, approvals, execution results.
- Has step-level observability: duration, input/output/total tokens, estimated
  cost, retry count, tool-call count, status, and stuck/degraded reason.
- Has run-level summary: total duration, total tokens, total estimated cost,
  slowest step, failed steps, and suspected loops.

Conversation operations:

- Create.
- Rename.
- Auto-generate title.
- Auto-generate tags.
- Manually edit tags.
- Archive.
- Physical delete chat-layer content.

Automatic tags must be context-specific:

- service, cluster, namespace, team
- alert type
- risk
- status
- source
- evidence/tool domain

Examples:

- `checkout`
- `prod-a`
- `latency`
- `waiting-approval`
- `human-started`
- `openobserve`

Deletion rules:

- Ordinary chat messages and `/btw` messages may be physically deleted.
- Responsibility-chain records must not be deleted.
- Deleting a conversation keeps a tombstone:
  `conversation_id`, `deleted_by`, `deleted_at`, `reason`,
  `linked_incident_ids`, `linked_approval_ids`, `linked_execution_ids`.

## Streaming

Real-time run output uses SSE, not WebSocket.

Endpoint:

- `GET /api/agent-runs/:runId/events`

Rules:

- Frontend first loads a snapshot: `GET /api/agent-runs/:runId`.
- Frontend then connects to SSE.
- Refreshing the page resumes the same run.
- `Last-Event-ID` supports reconnect.
- Events are persisted, not memory-only.
- SSE emits only events the current user is authorized to view.
- Event payloads are redacted.

Event types include:

- `run_started`
- `agent_message_delta`
- `agent_phase_changed`
- `tool_call_started`
- `tool_call_delta`
- `tool_call_finished`
- `evidence_added`
- `risk_classified`
- `approval_requested`
- `approval_decided`
- `execution_started`
- `preflight_finished`
- `mutation_finished`
- `post_check_finished`
- `step_stuck`
- `run_stuck`
- `run_finished`
- `run_failed`

Step and run events should carry enough metadata for the UI to show where time,
tokens, cost, retries, and tool calls were spent without exposing hidden prompts
or secrets.

## Evidence and OpenObserve

Evidence is a first-class model. The user should not need to jump across
Prometheus, Loki, topology, trace, and K8s screens to understand an incident.
Evidence is presented as an investigation process graph, timeline, call chain,
or run step list with clickable nodes. It is organized by the Agent
investigation flow first, and by source type second.

Evidence must not be shown as raw JSON by default. Node detail views show
summaries, charts, redacted snippets, scope, time range, refs, and why the
evidence mattered. Raw logs and raw tool responses are not part of the default
experience.

Evidence types:

- metrics
- logs
- traces
- K8s state/events
- topology/dependency
- changes
- host data, future
- tool output

OpenObserve is the first-class observability backend for the next version:

- OpenObserve provides logs, metrics, and traces.
- Gateway queries OpenObserve.
- Browser never receives OpenObserve tokens.
- Gateway applies user scope to queries.
- Gateway redacts results.
- Gateway audits evidence queries.
- Existing Prometheus/Loki/Topology MCP services remain compatibility/fallback
  paths during migration.

OpenObserve data must map to scope:

- `cluster`: `k8s_cluster_name` or `cluster`
- `namespace`: `k8s_namespace_name` or `namespace`
- `service`: `service_name` or `service`
- `team`: `team`
- `environment`: `deployment_environment`
- trace identity: `trace_id`, `span_id`

Missing scope fields fail closed for non-admin users.

Evidence API:

- `GET /api/incidents/:incidentId/evidence`
- `GET /api/agent-runs/:runId/evidence`
- `POST /api/evidence/query`

`POST /api/evidence/query` supports manual or agent-triggered evidence lookup:

```json
{
  "scope": {
    "cluster": "prod-a",
    "namespace": "default",
    "service": "checkout",
    "team": "payments"
  },
  "types": ["metrics", "logs", "traces", "k8s", "changes"],
  "time_range": {
    "from": "...",
    "to": "..."
  },
  "query": "checkout 5xx error"
}
```

Query permissions:

- `viewer`, `operator`, and `approver` use templated queries by default.
- Scoped `viewer`, `operator`, and `approver` users can see summaries, process
  graphs, redacted snippets, and refs inside their scope.
- `admin` and `auditor` may use advanced queries, still scope-limited and
  audited.
- `admin` and `auditor` may see more structured evidence detail, but secret
  redaction always applies.
- Agent uses allowlisted tool templates only.
- All queries have time range, row count, and timeout limits.

Redaction:

- OpenObserve ingestion/query redaction should be enabled when available.
- Gateway redaction is still mandatory.
- Secrets, tokens, passwords, authorization headers, and Kubernetes Secret
  contents are never shown.
- Admin and auditor cannot bypass secret redaction.
- A later audited raw-evidence path may be added for debugging, but it is not a
  default MVP user experience.

## Incident Workbench

`/incidents/:incidentId` is the primary Incident Workbench and evidence-first.

Layout:

- Top: incident summary, status, impact, owner, risk.
- Left: agent chat, investigation start, `/btw` side questions.
- Center: investigation/evidence process graph with clickable nodes for metrics,
  logs, traces, K8s, topology, changes, and tool calls.
- Right: diagnosis conclusion, recommended actions, approval status, and
  responsibility-chain summary.

Required incident controls:

- Pause run.
- Terminate run.
- Mark manual takeover.
- Add human note/conclusion.
- Restart a new run.
- Block new approvals for the current run.
- Resolve incident.
- Reopen incident.

Agent pause does not interrupt an already-approved execution. Execution cancel
or rollback must use execution-specific rules.

## Run Concurrency and Locks

Agent runs and mutations need explicit concurrency control.

Rules:

- Each incident has at most one active mainline run by default.
- An incident can have multiple `/btw` side threads.
- When a user starts a run while another active run exists, the UI offers:
  continue current, start a new run, or terminate the old run.
- Global agent concurrency limit is configurable.
- Per-cluster mutation concurrency limit is configurable.
- Mutations on the same target resource require a lock.
- Lock key is at least `backend + cluster + namespace + service/action target`.
- Lock state appears in timeline and audit.
- Lock timeout and recovery must be handled.

Stuck/dead-loop detection starts simple:

- Run exceeds its time budget.
- Step exceeds its time budget.
- The same tool, target, or query repeats without producing new evidence.
- Token or cost budget is exceeded.
- Retry count is exceeded.
- SSE shows no progress while the run is still marked running.

Stuck behavior:

- Mark the run or step as `stuck` or `degraded`.
- Stop further automatic tool calls for that run path.
- Preserve collected evidence and responsibility records.
- Ask the user to continue, terminate, start a new run, or take over manually.
- Do not auto-terminate a production mainline run by default.
- Do not cancel an already-approved or already-started mutation.

## Human Feedback

The Console records lightweight human feedback.

Feedback targets:

- Diagnosis conclusion: correct, partially correct, wrong.
- Evidence: useful, irrelevant, wrong.
- Action proposal: accepted, rejected, risk misclassified.
- Report: accurate, needs revision.

Rules:

- Feedback is stored on the run.
- Feedback can feed report generation.
- Feedback can be reviewed during audit and postmortem.
- First version does not perform automatic model training.
- Feedback is later useful for prompts, rules, and evaluation datasets.

## Knowledge Base

The Knowledge Base is an incident-experience store, not a generic document wiki.
It helps diagnosis, report generation, and future investigation prioritization.
It does not lower action risk, bypass policy, or bypass approval.
There is no standalone Knowledge Base management page in the MVP.

Knowledge entry fields:

- Scope: service, team/system, global, and optional environment/cluster
  constraints.
- Symptoms.
- Known root cause.
- Recommended checks.
- Recommended actions.
- Evidence refs.
- Status: candidate, approved, deprecated, blocked.
- Owner.
- Updated time.

Reuse rules:

- Knowledge is reusable across dev, staging, and production by default because
  the business system is the same.
- Environment or cluster only narrows reuse when explicitly marked.
- Retrieval priority is service-specific, then team/system-specific, then
  global.
- Agent may retrieve approved entries as diagnosis context.
- Process nodes show Knowledge Base refs when they influence the investigation.

Learning rules:

- Candidates can be generated from resolved incidents, published reports, human
  feedback marked correct, or successful action plus post-check.
- Candidates are not auto-approved.
- Service/team owner can approve entries.
- Admin can approve for convenience, marked as
  `approved_by_admin_override=true` and audited.
- Knowledge candidates do not automatically change runbook skeletons.

## Failure and Degraded Operation

Agent and evidence failures are local and recoverable, not whole-page failures.

Failure stages:

- Planning.
- Evidence query.
- Evidence parsing.
- Risk classification.
- Approval.
- Execution.
- Report generation.

User actions:

- Retry current step.
- Skip a failed evidence source.
- Take over manually.
- Start a new run.
- View sanitized error details.

Rules:

- Errors must not expose secrets.
- OpenObserve failure should not block K8s evidence or previously collected
  evidence.
- Feishu notification failure must not block Console approval.
- A failed evidence source should not crash the incident page.
- Failures are written to timeline and audit.

## Actions, Grants, and Execution

All changes are represented as Actions, regardless of backend.

Backends:

- `k8s`
- `notification`
- `ticket`
- `deployment`
- `feature_flag`
- `host`
- `custom_webhook`

First implementation supports:

- K8s read.
- K8s allowlisted mutation.
- Notification.

Future backends, including host execution, must still use Action -> policy ->
grant -> execution -> audit.

Action risk levels:

- `read_only`
- `low`
- `medium`
- `high`

Action types:

- `k8s_read`
- `restart_deployment`
- `scale_deployment`
- `rollback_deployment`
- `patch_config`
- `toggle_feature`
- `notify_only`

Gateway policy is the final risk classifier. Agent can suggest but cannot decide
final risk.

Grant rules:

- Every mutation uses a one-time grant.
- Human-approved actions create `human_approval_grant`.
- Policy-auto-allowed low-risk actions create `policy_grant`.
- Grants are bound to the frozen action hash and target scope.
- Grants can be consumed once.
- Success, failure, and preflight failure all consume the grant.
- Idempotency restores the same execution; it does not authorize a new one.

Execution order:

```text
preflight -> mutation -> post-check
```

Preflight failure blocks mutation. Post-check failure marks
`rollback_required`.

## Approvals

Approval is created automatically by Gateway when policy requires it.

Approval flow:

```text
Agent proposal -> Gateway policy classification -> approval request
-> human approve/reject -> Gateway automatic execution
-> preflight -> mutation -> post-check -> audit/timeline
```

Rules:

- Approval pending timeout is 30 minutes.
- Approval grants one execution of one frozen action.
- Approval does not authorize a class of actions.
- Approval passed means Gateway immediately executes the frozen action.
- Approval details must show evidence, target, risk, preflight, rollback plan,
  expected impact, requester, agent, and responsibility chain.
- Reject requires a remark.
- Approve should require a remark for traceability.
- Self-approval is allowed when policy permits, but it is marked and audited.
- First version uses one approver.
- If no approver is available, status is `blocked:no_approver` and admin is
  notified.

Approval page layout:

- Left: frozen Action card with action type, risk, target, preflight, rollback,
  expected impact, and action hash.
- Center: evidence summary and investigation process nodes. No raw JSON dump and
  no re-entering execution parameters.
- Right: responsibility chain, associated agent run, incident, approval history,
  approve/reject, and execution progress.

## Audit and Responsibility Chain

Audit is primarily about responsibility, not raw log volume.

It must answer:

1. What did the agent request?
2. Why did the agent request it?
3. Who approved it?
4. What did Gateway execute?
5. What happened afterwards?

`/audit` default view is responsibility chains.

Tabs:

- Responsibility chains.
- Raw logs.
- Deleted conversation records.

Responsibility chain fields:

- Time.
- Incident.
- Conversation/run.
- Agent.
- Requested action.
- Risk.
- Target resource.
- Approver.
- Approval decision.
- Gateway execution result.
- Responsibility status.

Details include:

- Agent request.
- Evidence refs.
- Risk classification.
- Frozen action/hash.
- Approver identity snapshot.
- Approval remark.
- Preflight/mutation/post-check.
- Notifications.
- Delete tombstone.
- Raw audit refs.

Immutable responsibility records:

- Action request.
- Risk classification.
- Evidence refs.
- Approval request.
- Approval decision.
- Approver identity snapshot.
- Frozen action payload/hash.
- Execution record.
- Preflight/mutation/post-check result.
- Audit log.

## Reports

Incident reports are HTML-first.

Route:

- `/incidents/:incidentId/report`

Capabilities:

- Agent generates an HTML post-incident report.
- Markdown export is secondary.
- Reports have versions.
- Agent-generated reports start as draft.
- Human publishes after review.
- Published reports are immutable; changes create a new version.
- Reports are printable.
- Reports can export HTML.
- Reports can be shared by internal authenticated links only.
- No public unauthenticated share links.

Report sections:

- Summary.
- Timeline.
- Impact.
- Root cause.
- Trigger.
- Remediation process.
- Agent actions and human approvals.
- Evidence references.
- Recovery validation.
- Follow-up items.
- Responsibility chain.

Reports must not invent missing evidence. Unknowns stay unknown.

## Notifications

Notifications have three layers:

- Console notification center.
- Feishu/external notifications.
- Page-level realtime toast/SSE events.

Triggers:

- New incident.
- Agent waiting for human input.
- Approval pending.
- Approval approved/rejected.
- Execution succeeded/failed/rollback_required.
- Settings/users permission changes.
- `blocked:no_approver`.

Rules:

- Console notifications are scope-filtered.
- Feishu mapping is service/team to channel.
- External approval notifications link directly to `/approvals/:approvalId`.
  They must not route users through Notification Center or a legacy approval
  center path.
- First version does not need complex personal notification preferences.
- Notification delivery records link into audit and responsibility chains.

## Global Search

First version includes Console-wide search.

Searchable objects:

- incidents
- conversations
- agent runs
- approvals
- audit chains
- users
- clusters
- namespaces
- services
- teams

Rules:

- Results are permission-filtered.
- Results link to real routes.
- Search uses a Gateway API, not local frontend joins.
- Sensitive object access still follows normal audit/query rules.

## Data Retention

Retention follows value and accountability.

- Responsibility-chain audit: long term, at least one year by default.
- Approval and execution records: same retention as audit.
- Frozen action, hash, and approver identity snapshot: long term.
- Evidence summaries and refs: long term.
- Raw evidence: short term, 30 days by default.
- Conversation chat: user-manageable archive/delete.
- Reports: long term.
- OpenObserve raw logs, metrics, and traces follow OpenObserve retention policy.

## Frontend UX Rules

- Routes are real URLs.
- No marketing homepage.
- No `/overview` placeholder.
- Login goes to the requested route or default Incident Workbench route.
- Evidence is directly visible in incident, approval, and agent run views.
- Evidence is shown as investigation process graphs, timelines, call chains, or
  clickable nodes by default, not raw JSON.
- Dangerous actions are hidden when unauthorized.
- Non-dangerous unavailable actions can be disabled with a reason.
- Approval buttons appear only for eligible approvers.
- Self-approval is clearly marked.
- Auditor gets read-only views only.
- Settings/users write controls are admin-only.
- Runbook management in MVP is view plus enable/disable, not a page editor.

## Mobile

Mobile support focuses on urgent approval and incident awareness.

Required on mobile:

- Login.
- Incident summary.
- Approval detail.
- Evidence summary.
- Approve/reject.
- Approval remarks.
- Execution status.
- Opening notification links.

Not required to be comfortable on mobile in the first version:

- Complex evidence analysis.
- Settings management.
- User management.
- Full desktop investigation workspace.

## Accessibility

Basic accessibility is a required acceptance criterion.

Rules:

- Form fields have labels.
- Keyboard tab order works for key flows.
- Focus state is visible.
- Risk/status is not conveyed by color alone.
- Dangerous actions use specific labels, not generic "confirm".
- Modal focus is managed.
- Loading, error, and empty states use clear text.
- Evidence tabs are keyboard-accessible.
- Font size and contrast are readable.
- Animation must not interfere with operation.

## Visual Design Direction

This is an SRE/AIOps operations console.

Design direction:

- Dense but clear.
- Data and evidence first.
- Professional operations console, not a marketing page.
- No large hero page.
- No decorative card pile.
- No ornamental gradient-heavy style.
- High-contrast state and risk indicators.
- Scannable evidence panels.
- Clear approval actions.
- Desktop supports complex investigation.
- Mobile supports critical approval.

Implementation should use an appropriate frontend design skill before UI work,
such as `design-taste-frontend`, `redesign-existing-projects`, or
`frontend-design`.

## Test and Acceptance

Passing a build is not enough.

Backend tests:

- RBAC and scope.
- Users and settings.
- Cluster configuration and degraded connector behavior.
- Evidence query scope and redaction.
- SSE replay.
- Step/run stuck detection events.
- Action, grant, and idempotency.
- Approval and self-approval.
- Responsibility-chain audit.
- Settings version and rollback.
- OpenObserve connector failure degradation.
- Knowledge candidate creation and approval boundaries.

Frontend tests:

- Build/typecheck.
- Route refresh.
- Login `next`.
- Permission-filtered navigation.
- 403 and 404 pages.
- Mobile approval flow.
- Evidence panel loading/empty/error states.
- Evidence process graph/node detail states.
- Agent timeline streaming mock.
- Stuck/degraded run state.
- Approval approve/reject flow.
- Settings diff/confirm flow.
- Users role/scope edit flow.

End-to-end smoke:

```text
login -> open incident -> start agent run -> see live evidence/tool timeline
-> request service restart -> approve -> Gateway executes mock
-> audit shows responsibility chain
```

## Delivery Slices

Work should be split by vertical user value, not by frontend/backend layers.

Planned slices:

1. Login, routes, RBAC, and user management.
2. Cluster management and OpenObserve health wiring.
3. Agent conversations, human-started tasks, and SSE timeline.
4. Action/grant/approval/automatic execution.
5. Incident workbench, takeover, resolve, and reopen.
6. Responsibility-chain audit.
7. Settings and policy management.
8. HTML post-incident report.
9. Knowledge Base candidates and approved-entry retrieval.
10. Notification center and external notifications.

MVP first batch:

1. Login, routes, RBAC, and user management.
2. Cluster management, OpenObserve health, and scoped evidence query.
3. Incident Workbench with evidence process graph and Agent Run detail.
4. Runbook Orchestrator, conversations, human-started tasks, and SSE timeline.
5. Action/grant/approval/automatic execution.
6. Responsibility-chain audit.

This first batch delivers the core loop:

```text
login -> incident workbench -> start investigation or request action
-> runbook-driven Agent Run -> evidence process graph
-> approval when needed -> Gateway auto-execution -> responsibility-chain audit
```
