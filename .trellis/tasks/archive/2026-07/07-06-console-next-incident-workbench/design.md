# Console Next incident workbench design

## Scope

This slice delivers the evidence-first incident workbench and incident/run
controls.

## Architecture

- Reuse `incident_store` for incident status, timeline, evidence, diagnosis, and
  reopen behavior.
- Reuse `agent_run_service` for incident-linked runs and active mainline checks.
- Reuse existing Gateway evidence/action/approval/execution APIs by composing
  references in a workbench snapshot instead of duplicating those stores.
- Add small Gateway routes for:
  - incident workbench snapshot
  - incident controls that append timeline and audit rows
  - incident-linked run start with one-active-mainline default
- Keep all browser calls same-origin Gateway `/api/*`.

## Data flow

1. Console loads `/api/incidents/active` for route-safe incident list.
2. Incident detail loads `/api/incidents/{id}/workbench`.
3. Workbench response composes incident row, timeline, evidence, diagnosis
   process, linked runs, approvals/execution refs, and permission flags.
4. Control POSTs validate incident scope, write incident timeline and audit, and
   update status/operator where needed.
5. Starting a run checks active incident-linked mainline runs before creating a
   new run.

## Confirmed decisions

- Desktop uses a three-column workbench:
  - chat/run controls
  - evidence workbench
  - decision/responsibility panel
- Mobile only shows incident summary and links in this slice; full mobile
  workbench is out of scope.
- `pause`, `terminate`, `manual takeover`, `resolve`, and `reopen` all write
  timeline and audit events.
- `block new approvals` applies only to the current run.
- Historical approvals are not changed by `block new approvals`.
- Agent pause does not interrupt an already-approved execution.
- Starting a run when another mainline run is active offers continue current,
  start new, or terminate old.

## Contracts

- `GET /api/incidents/{incident_id}/workbench` returns independent panels; a
  panel failure is represented locally instead of failing the whole response.
- `POST /api/incidents/{incident_id}/controls` accepts `pause_run`,
  `terminate_run`, `manual_takeover`, `human_note`, `restart_run`,
  `block_approvals`, `resolve`, and `reopen`.
- Controls require `PERMISSION_VIEW_INCIDENT` for the incident scope.
- Controls write an `incident_events` row and a Gateway audit row.
- `resolve` updates incident status to `resolved`; `reopen` calls
  `incident_store.reopen_incident`.
- Pause/terminate are control-plane state events only in this slice; they do not
  cancel already-approved executions.

## Deferred

- Full mobile investigation workspace.
- Report generation.
- Audit chain deep dive.
