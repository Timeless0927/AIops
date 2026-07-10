# Tickets: AIOps Console Incident Workbench Redesign

Superseded by `../aiops-v1-model/tickets.md`. These unchecked items are historical and are not part of the current frontier.

Build the first-version AIOps Console around the Incident Workbench, based on `.scratch/aiops-console-redesign/PRD.md`.

Work the **frontier**: any ticket whose blockers are all done.

## Incident Workbench Shell And Route Pruning

**What to build:** The Console opens around incidents only. Users see the first-version route set, a pruned navigation model, and an Incident Workbench shell with compact status bar plus left/context, center/investigation, and right/conclusion columns.

**Blocked by:** None — can start immediately.

- [ ] Only first-version routes are exposed: `/login`, `/incidents`, `/incidents/:incidentId`, `/incidents/:incidentId/report`.
- [ ] Top-level entries for Agent Runs, Runbooks, Approvals, Audit, Policies, Clusters, Users, Settings, Search, and KB are removed from primary navigation.
- [ ] Incident detail renders a compact status bar instead of a large title card.
- [ ] Desktop layout renders left context, center investigation, and right conclusion/actions columns.
- [ ] Narrow layout stacks conclusion summary, investigation process, then incident context.
- [ ] Loading and empty states keep the workbench layout stable and readable.

## Incident Workbench API Adapter

**What to build:** Incident detail loads from a single incident-level workbench API that shapes existing incident, run, evidence, conclusion, action, and resource state into the product model the redesigned UI needs.

**Blocked by:** Incident Workbench Shell And Route Pruning.

- [ ] `GET /api/incidents/:incidentId/workbench` returns event facts, resource context, timeline, human notes, current investigation thread, current judgment, recommended actions, responsibility summary, and report-ready data.
- [ ] The workbench payload uses incident/product language, not standalone Agent Run product language.
- [ ] Existing run/event/evidence/resource stores can be reused behind the adapter without exposing their internal shape to the UI.
- [ ] Permission-limited data is represented explicitly instead of as fake empty data.
- [ ] Connector offline and resource unbound states are represented in the workbench payload.
- [ ] Backend tests cover the complete workbench payload shape at the API boundary.

## Incident-Scoped Investigation Controls

**What to build:** Users control the investigation from the incident page using incident-level APIs and a fixed state machine. Starting investigation does not create duplicate active threads.

**Blocked by:** Incident Workbench API Adapter.

- [ ] Incident-level APIs exist for start, pause, terminate, and human takeover.
- [ ] Starting an investigation returns the existing running thread when one already exists.
- [ ] UI shows `not_started`, `running`, `paused`, `takeover`, `terminated`, `finished`, and `failed` states with the agreed controls.
- [ ] "开始调查" changes immediately to "调查中" and is disabled while running.
- [ ] Human takeover stops Agent automatic progression, records responsibility, and preserves existing evidence/messages.
- [ ] The frontend no longer calls `/api/agent-runs/*` directly for investigation controls.
- [ ] Tests cover state transitions and duplicate-start prevention.

## Visible Agent Messaging And SSE Feedback

**What to build:** Sending a message inside an incident produces immediate visible feedback and event-scoped streaming updates for Agent replies, tool calls, evidence changes, and failures.

**Blocked by:** Incident-Scoped Investigation Controls.

- [ ] Sending a message immediately renders the user message with a sent/pending state.
- [ ] The UI shows "Agent 正在处理你的问题" while waiting.
- [ ] If no Agent event arrives within 10 seconds, the UI shows a readable waiting state.
- [ ] Event-scoped SSE emits the required investigation event names.
- [ ] Agent response deltas, tool call start/finish, evidence step updates, failures, and completion update the visible investigation process.
- [ ] Failed sends or Agent failures show a readable reason and retry action.
- [ ] `/btw`, promote, and parallel discussion threads are not exposed.
- [ ] Tests cover message submission, waiting state, streaming updates, and failure state.

## Evidence Step Contract And Readable Redaction

**What to build:** Evidence is presented as readable Evidence Steps with structured redacted samples. Default UI never renders raw JSON or process-node dumps.

**Blocked by:** Incident Workbench API Adapter.

- [ ] Workbench evidence uses the Evidence Step contract: title, status, finding, why, source, query summary, result summary, impact, time range, scope, refs, samples, and optional error.
- [ ] Completed Evidence Steps are understandable without expanding details.
- [ ] Failed, skipped, and empty evidence states include concrete reasons.
- [ ] Redacted samples use structured summaries and fields with redaction reasons.
- [ ] Long logs preserve operational context instead of relying on hard string truncation.
- [ ] Default UI does not render raw JSON, Python dicts, or full Kubernetes objects.
- [ ] User-visible primary text does not expose internal event names or enum names.
- [ ] Tests cover evidence step readability, redaction reasons, and raw JSON avoidance.

## Evidence-Grounded Actions And Report Preview

**What to build:** Recommended actions and incident reports are grounded in Evidence Steps. The report page remains a lightweight incident output, not a report management system.

**Blocked by:** Visible Agent Messaging And SSE Feedback; Evidence Step Contract And Readable Redaction.

- [ ] Each recommended action references at least one Evidence Step or evidence ref.
- [ ] Action cards show 1-3 evidence step titles as "依据".
- [ ] Uncertain evidence marks actions as needing human confirmation.
- [ ] Missing or failed key evidence produces next-check recommendations instead of confident remediation.
- [ ] The report page shows known facts, timeline, evidence summaries, conclusion, human notes, follow-up items, and explicit unknowns.
- [ ] Reports cite Evidence Steps instead of loose evidence ids.
- [ ] Report versioning, publish flow, public sharing, and KB candidate generation are not implemented.
- [ ] Tests cover evidence-grounded action display and report preview content.

## Connector-Registered Resource Binding Model

**What to build:** Incidents use a binding-first resource model. Clusters come from Connector registration, and services/teams/deployment targets come from discovery or service catalog data plus human confirmation.

**Blocked by:** Incident Workbench API Adapter.

- [ ] The Console cannot create a new cluster from free-text admin input.
- [ ] Registered clusters expose editable display name, environment, owner team, and policy/notes only.
- [ ] Connector runtime reporting owns online/offline/degraded state, connector identity, heartbeat, and runtime failure summary.
- [ ] Service, Team, and DeploymentTarget are represented as binding concepts.
- [ ] Service bindings can be confirmed or corrected only from discovered resources, CMDB/service catalog records, or alert-derived candidates.
- [ ] Incidents can show resource binding status in the left column.
- [ ] Unbound incidents remain visible and show "资源未绑定".
- [ ] Tests cover Connector-only cluster identity and binding status behavior.

## Team-Based Authorization And Unbound Incident Behavior

**What to build:** Access to incidents and actions resolves through User -> Team -> Service -> DeploymentTarget bindings. Unbound incidents degrade safely.

**Blocked by:** Connector-Registered Resource Binding Model.

- [ ] Users can receive team-scoped role bindings such as team member, team operator, and team approver.
- [ ] Platform admin and platform auditor roles remain platform-level roles and do not bypass action policy.
- [ ] Permission checks resolve through team ownership of services and service deployment targets.
- [ ] Users without a connected binding cannot access scoped incident data unless a platform role explicitly allows it.
- [ ] Unbound incidents allow safe read-only investigation based only on reliable alert-label scope.
- [ ] Unbound incidents disable execution-class suggested actions.
- [ ] The UI shows "需要确认资源归属" when ownership blocks actionability.
- [ ] Tests cover team-based access, unbound incident visibility, and disabled execution actions.
