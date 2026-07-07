# Console Next incident workbench implementation plan

## Order

1. Add Gateway workbench routes
   - `GET /api/incidents/{incident_id}/workbench`
   - `POST /api/incidents/{incident_id}/controls`
   - Reuse `_incident_resource_scope`, `_authorize`, `incident_store`,
     `agent_run_service`, `approval_service`, and `approval_execution_service`.

2. Implement workbench snapshot
   - Return incident summary, timeline, evidence, diagnosis process, linked
     agent runs, approval/execution refs, responsibility summary, and permission
     flags.
   - Panel failures become `{status:"failed"}` panel entries; do not blank the
     entire page.

3. Implement controls
   - Actions: `pause_run`, `terminate_run`, `manual_takeover`, `human_note`,
     `restart_run`, `block_approvals`, `resolve`, `reopen`.
   - Each control writes timeline and audit.
   - `resolve` updates status to `resolved`; `reopen` reopens resolved incidents.
   - Pause/terminate do not cancel already-approved executions.

4. Enforce active mainline default
   - Starting/restarting an incident-linked run checks existing active
     incident-linked runs.
   - Default response offers continue current; `mode=start_new` creates a new run,
     `mode=terminate_old` terminates old run then creates.

5. Add Console pages
   - Replace `/incidents` and `/incidents/:incidentId` placeholders with local
     components.
   - Detail renders summary, evidence, timeline, diagnosis, run controls,
     approvals/execution status, and responsibility summary.

6. Tests
   - Add `tests/test_gateway_incident_workbench.py` using real
     `ThreadingHTTPServer` + `urllib`.
   - Extend `tests/test_aiops_console_web.py` for Gateway-only workbench calls,
     Chinese labels, controls, and route guards.

## Validation commands

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py`
- `rtk test pytest -q tests/test_gateway_incident_workbench.py`
- `rtk test pytest -q tests/test_gateway_identity_rbac.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk npm run build` from `apps/aiops_console_web`

## Review gates

- Browser calls Gateway only.
- `_authorize` runs before incident data/control mutation.
- Missing scope fails closed for non-admin.
- Control actions write timeline and audit.
- Panel failure does not fail the whole workbench response.
- No new worker, broker, or agent runtime.

## Out of scope

- Report generation.
- Audit-chain deep dive.
- Cancelling already-approved executions.
- Full mobile investigation workspace.
