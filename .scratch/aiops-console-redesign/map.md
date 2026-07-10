## Destination

Produce an agent-ready product specification for rebuilding the AIOps Console first version around the Incident Workbench. The spec should replace the old page model, keep the old backend only as a reusable interface source, and be ready to feed `/to-spec`.

## Notes

- Effort: AIOps Console product and UI redesign.
- Old implementation reference: `/root/aiops/AIops`.
- Planning only: do not implement in this map.
- Language: Chinese for human-facing docs and UI copy; code identifiers and API fields stay English.
- First version is event-workbench-first. Avoid feature sprawl.
- Use `/grilling` and `/domain-modeling` when resolving HITL product decisions.

## Decisions so far

- [Define first-version page scope](issues/01-define-first-version-page-scope.md) — First version keeps only login, incidents, incident detail, and lightweight incident report routes; Agent Runs, approvals, audit, management, search, runbooks, and KB are removed as top-level product surfaces.
- [Design incident workbench information architecture](issues/02-design-incident-workbench-information-architecture.md) — Incident detail uses a compact status bar and a 280px / 1fr / 360px workbench: left context, center investigation steps before chat, right conclusion/actions, with explicit empty/loading/error states.
- [Define agent investigation thread model](issues/03-define-agent-investigation-thread-model.md) — UI exposes an incident-scoped investigation thread with incident-level APIs, visible message/Agent/tool/evidence feedback, explicit SSE events, and a fixed not_started/running/paused/takeover/terminated/finished/failed state machine.
- [Define evidence step and redaction contract](issues/04-define-evidence-step-and-redaction-contract.md) — Evidence is presented as readable Evidence Steps with structured redacted samples; default UI never renders raw JSON, and recommended actions must cite supporting steps.
- [Define connector registration and resource bindings](issues/05-define-connector-registration-and-resource-bindings.md) — Clusters come only from Connector registration; services/teams/deployment targets use discovery plus confirmation; user access resolves through user-team-service-deployment bindings, not free-text scopes.
- Mobile first-version scope is responsive incident awareness only: show status, conclusion summary, investigation steps, and blocking confirmations. Complex investigation and resource management stay desktop-first.
- The rebuild is primarily a Web/product experience redesign, not a full backend rewrite.
- First version centers on the Incident Workbench after login.
- Incident detail uses three stable columns: incident context, investigation process, conclusion/actions.
- Agent Runs are internal records, not a first-class UI concept; the UI shows an event-scoped investigation thread.
- `/btw` side threads are out of first-version scope.
- Cluster identity comes from Connector registration; admins may edit display name, environment, ownership, and policy, but not forge identity or runtime status.
- Resource permissions use bindings among users, teams, services, namespaces, and clusters, not free-text fields on users.
- Evidence is shown as investigation steps with expandable details, not a complex unreadable process graph.
- Redaction must preserve useful operational context while removing secrets and sensitive fields.

## Not yet specified

- Which old Gateway APIs are reusable as-is, which need thin adapters, and which old endpoints should disappear from the first-version UI. This belongs in the next `/to-spec` or implementation planning pass, not another product-shape wayfinding ticket.

## Out of scope

- Automatic production mutation execution in the first Console redesign spec.
- Standalone Agent Runs product surface.
- Runbook management page with enable/disable controls.
- KB candidate approval workflow.
- Uncontrolled global search across every object.
- Full user-management back office beyond resource/role bindings needed by the first version.
