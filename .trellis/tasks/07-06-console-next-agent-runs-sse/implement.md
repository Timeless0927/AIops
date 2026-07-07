# Console Next conversations/agent-runs/SSE implementation plan

## Order

1. Add a small Gateway-owned run service
   - Create `apps/aiops_k8s_gateway/agent_run_service.py`.
   - Use one SQLite database under `AIOPS_DATA_DIR`.
   - Tables: conversations, agent_runs, run_events.
   - Store stable JSON for scope, tags, payload, evidence refs, action refs,
     approval refs, and execution refs.
   - Redact secret/token/password/authorization/API-key fields and values.

2. Add Gateway API routes
   - `GET /api/agent-runs`: list runs visible to the actor.
   - `POST /api/agent-runs`: create a conversation/run from incident or manual
     scope.
   - `GET /api/agent-runs/{run_id}`: snapshot with conversation, run, timeline,
     evidence/action/approval/execution refs, and permissions.
   - `GET /api/agent-runs/{run_id}/events`: persisted event replay, optional
     `after_id`.
   - `GET /api/agent-runs/{run_id}/stream`: SSE replay using `Last-Event-ID`.
   - `POST /api/agent-runs/{run_id}/messages`: append mainline or `/btw`
     message event.
   - `POST /api/agent-runs/{run_id}/promote`: promote a side-thread event into
     mainline.
   - Archive/delete chat-layer conversation controls without deleting immutable
     run events.

3. Keep execution deliberately small
   - Creating a run appends `run_created` and `run_started` events.
   - Message append creates timeline events; `/btw` marks `thread_type=side`.
   - Promotion creates a new mainline event referencing the side event id.
   - No real agent runtime, no background worker, no cross-process pub/sub in
     this slice.

4. Add Console pages
   - Replace `/agent-runs`, `/agent-runs/new`, and `/agent-runs/:runId`
     placeholders with local components in `App.tsx`.
   - Browser calls Gateway only.
   - Snapshot loads before opening `EventSource`.
   - Render timeline, evidence refs, side-thread markers, promotion, loading,
     empty, error, unauthorized, and stale/reconnect states.

5. Tests
   - Add `tests/test_gateway_agent_runs_sse.py` using real
     `ThreadingHTTPServer` + `urllib`.
   - Cover create/list/snapshot, scope authorization, missing scope fail-closed,
     redaction, event replay, `Last-Event-ID`, `/btw`, promotion, archive/delete
     chat controls, and audit rows.
   - Extend `tests/test_aiops_console_web.py` for Gateway-only agent-run calls,
     `EventSource`, Chinese labels, route permissions, and no direct internal
     service URLs.

## Validation commands

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/agent_run_service.py`
- `rtk test pytest -q tests/test_gateway_agent_runs_sse.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk test pytest -q tests/test_gateway_identity_rbac.py`
- `rtk npm run build` from `apps/aiops_console_web`

## Review gates

- Browser calls Gateway only.
- `_authorize` runs before run/conversation data is returned or mutated.
- Non-admin missing scope fails closed.
- Snapshot is loaded before SSE.
- SSE streams only persisted, authorized, redacted events.
- `/btw` side-thread state cannot mutate mainline without explicit promotion.
- Raw chain-of-thought and secrets are not persisted or shown.
- No new dependency, broker, worker, or full agent runtime.

## Rollback points

- Backend is additive: remove `/api/agent-runs*` routes and the new service file.
- Frontend can fall back to current placeholders without touching auth/session.
- SSE stream can be disabled while keeping snapshot/event replay route.

## Out of scope

- Real background agent orchestration.
- Multi-agent branches.
- Cross-process live event fanout.
- Action execution; later action/grant/approval slice owns execution.
