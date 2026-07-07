# Console Next OpenObserve evidence implementation plan

## Order

1. Add a small Gateway-owned evidence query service
   - Create `apps/aiops_k8s_gateway/evidence_service.py`.
   - Keep raw evidence out of SQLite; return redacted summaries, refs, status,
     and small samples only.
   - Support request fields: `query_type`, `template`, `scope`, `time_range`,
     `limit`, `advanced_query`, and optional `incident_id` / `run_id`.
   - Clamp server-side limits: time range, row count, and backend timeout.
   - Reject non-admin/non-auditor advanced queries.
   - Fail closed when non-admin scope lacks cluster, namespace, service, or team.

2. Add backend adapters without a new service dependency
   - OpenObserve is primary when configured by env.
   - Use stdlib HTTP calls for OpenObserve.
   - If OpenObserve is unconfigured or fails, return partial evidence using
     current Prometheus, Loki, Kubernetes, and Topology compatibility seams where
     a safe fallback is available.
   - Agents use a fixed allowlist of templates only.

3. Add Gateway routes
   - `POST /api/evidence/query` -> manual scoped evidence lookup.
   - `POST /api/evidence/agent-query` -> allowlisted agent-template lookup.
   - Both require existing user session auth and Gateway scope checks.
   - Audit every request with requester, scope, query type/template, result, and
     request id.
   - Return per-source states so one failed backend does not blank the page.

4. Add reusable Console evidence panels
   - Replace relevant placeholders with a simple evidence page/panel surface.
   - Browser calls Gateway only.
   - Render metrics, logs, traces, Kubernetes, topology, changes, and tool output
     sections when present.
   - Show independent loading, empty, partial, error, unauthorized, and stale
     states.
   - Keep panel components local to `App.tsx` unless duplication actually appears.

5. Tests
   - Add `tests/test_gateway_evidence_openobserve.py` with real
     `ThreadingHTTPServer` Gateway tests for:
     - scoped templated query succeeds for allowed user
     - missing scope fails closed for non-admin
     - admin/auditor advanced query allowed; ordinary user denied
     - limit/timeouts are clamped
     - OpenObserve failures degrade to partial fallback
     - redaction removes secrets/tokens/passwords/auth headers/Kubernetes Secret
       content
     - audit rows include query metadata and request id
   - Extend `tests/test_aiops_console_web.py` for:
     - same-origin `/api/evidence/query`
     - no OpenObserve/internal service URLs in browser code
     - Chinese evidence panel labels and partial/error states

## Validation commands

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/evidence_service.py`
- `rtk test pytest -q tests/test_gateway_evidence_openobserve.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk test pytest -q tests/test_gateway_identity_rbac.py`
- `rtk npm run build` from `apps/aiops_console_web`

## Review gates

- Browser calls Gateway only.
- No OpenObserve token reaches the browser.
- Scope validation happens before backend query.
- Missing scope fails closed for non-admin users.
- Redaction is applied by Gateway even when backend claims redaction.
- Raw evidence is not stored in SQLite.
- Partial backend failure returns per-source degraded state.
- No new dependency or long-lived service.

## Rollback points

- Backend is additive: remove `/api/evidence/*` routes and the new service file.
- Frontend can fall back to placeholder/evidence-empty panels without touching
  auth/session.
- OpenObserve env can be unset; compatibility fallback remains safe.

## Out of scope

- Host evidence.
- Long-term raw evidence retention.
- Ordinary-user arbitrary query builder.
- Full incident workbench layout; later workbench slice owns page composition.
