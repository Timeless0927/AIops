# Console Next incident workbench and run controls

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver the evidence-first incident workbench and run controls. Incident detail
shows summary, status, impact, owner, risk, chat, evidence, timeline, diagnosis,
recommended actions, approval status, execution status, and responsibility
summary. Operators can pause, terminate, take over, restart runs, block new
approvals, resolve, and reopen incidents according to permissions.

The completed slice should be demoable as: operator opens an incident, sees
evidence/chat/timeline/action/approval panels, pauses a run, marks manual
takeover, resolves the incident, and reopens it with audit/timeline updates.

## User Stories Covered

- Parent stories 17, 21, 22, 23, 27, 37, 38, 41, and 55.

## Requirements

- `/incidents` and `/incidents/:incidentId` support permission-filtered incident
  history and detail.
- Incident detail layout is evidence-first.
- Required incident controls are pause run, terminate run, mark manual takeover,
  add human note/conclusion, restart a new run, block new approvals for current
  run, resolve incident, and reopen incident.
- Each incident has at most one active mainline run by default.
- An incident can have multiple `/btw` side threads.
- Starting a run when another active run exists offers continue current, start
  new run, or terminate old run.
- Agent pause does not interrupt an already-approved execution.
- Execution cancel or rollback uses execution-specific rules.
- Workbench panels degrade independently.
- All incident controls write timeline and audit events.

## Acceptance Criteria

- [ ] Incident list and detail are real routes with refresh-safe state.
- [ ] Incident detail shows summary, evidence, chat, timeline, diagnosis,
  recommended actions, approval/execution status, and responsibility summary.
- [ ] Pause, terminate, manual takeover, human note, restart run, block
  approvals, resolve, and reopen controls are permission-gated and audited.
- [ ] One-active-mainline-run default is enforced.
- [ ] Starting a run during active run offers the required choices.
- [ ] Side threads remain separate from mainline.
- [ ] Pausing an agent does not cancel already-approved execution.
- [ ] Panel failures do not blank the full incident page.
- [ ] Tests cover incident state transitions, run concurrency, permission
  gating, timeline/audit writes, and partial panel failures.

## Blocked by

- Console Next OpenObserve evidence query and panels.
- Console Next conversations, agent runs, and SSE timeline.
- Console Next action grants, approvals, execution, and locks.

## Further Notes

Keep this slice incident-workbench focused. Report generation and audit chain
detail belong to later slices.
