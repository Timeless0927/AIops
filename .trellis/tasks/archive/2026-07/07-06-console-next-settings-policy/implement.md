# Console Next settings/policy implementation plan

## Order

1. Add a small Gateway-owned settings store
   - Create `apps/aiops_k8s_gateway/settings_service.py`.
   - Use SQLite with the existing WAL, lock, retry, JSON-column pattern from
     `approval_service.py`.
   - Store versioned sanitized settings snapshots, not env vars.
   - Keep the first schema boring:
     - `settings_versions(version_id, version_number, settings_json, diff_json,
       created_by, created_at, change_summary, critical_confirmed, reload_required)`
     - `policy_hits(id, when_ts, actor, action_type, cluster, namespace, service,
       team, environment, decision, reason, settings_version)`
   - Seed version 1 lazily from built-in defaults when the DB is empty.

2. Define the minimum product settings contract
   - Sections returned to the UI:
     - `clusters`: cluster/environment rows; missing cluster resolves as `prod`.
     - `approval_policy`: prod/staging/dev/test behavior and self-approval flags.
     - `action_allowlist`: `restart_deployment`, `scale_deployment`,
       `rollback_deployment`.
     - `notifications`: non-secret channel enablement and delivery labels only.
     - `security`: session/CSRF display values only, no secrets.
     - `feature_flags`: booleans for later slices, default off.
   - Exclude secrets, LDAP bind password, tokens, DB paths, and internal URLs from
     every response.
   - Add `PERMISSION_MANAGE_SETTINGS` for admin-only writes; keep
     `PERMISSION_VIEW_SETTINGS` and `PERMISSION_VIEW_POLICY` for admin/auditor
     read routes.

3. Add Gateway routes
   - `GET /api/settings` -> current sanitized settings, current version, sections,
     reload/restart hints.
   - `POST /api/settings/preview` -> validate payload and return diff/critical
     flags without saving.
   - `POST /api/settings` -> admin-only save; require CSRF for cookie auth; create
     a new version and audit `settings_save`.
   - `POST /api/settings/rollback` -> admin-only rollback to previous version;
     create a new version and audit `settings_rollback`.
   - `GET /api/policies` -> active policy explanation, cluster environments,
     allowlists, and recent policy hits.
   - `POST /api/policies/test` -> optional first-slice affordance: classify one
     action/target and record a policy hit.

4. Wire policy explanation without executing mutations
   - Add a pure classifier in the settings service:
     - `prod`: all mutations require approval.
     - `staging`: low risk may receive a policy grant; medium/high require approval.
     - `dev/test`: low-risk auto execution is configurable.
     - unconfigured cluster: `prod`.
   - Do not change approval execution semantics in this child. Later action slices
     can consume the classifier.

5. Replace `/settings` and `/policies` placeholders
   - Fetch only same-origin Gateway APIs.
   - Settings page:
     - admin: edit, preview diff, exact critical confirmation, save, rollback.
     - auditor: read-only view.
   - Policies page:
     - readable policy summary, cluster environments, allowlist, recent hits, and
       test-policy result.
   - Use the existing `readJson`/`writeJson` helpers and capability guards.
   - Keep controls simple: text inputs/selects/checkboxes; no new UI dependency.

6. Tests
   - Add `tests/test_gateway_settings_policy.py` for:
     - version seed/current read
     - admin save creates version and audit
     - rollback creates a new audited version
     - auditor can read but cannot write
     - secret-like fields absent from responses
     - critical confirmation required
     - unconfigured cluster resolves as `prod`
     - policy hit visibility
   - Extend `tests/test_aiops_console_web.py` for:
     - `/api/settings` and `/api/policies` same-origin calls
     - CSRF write calls
     - no forbidden internal service URLs
     - capability strings and Chinese-first labels.

## Validation commands

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/settings_service.py aiops/domain/identity.py`
- `rtk test pytest -q tests/test_gateway_settings_policy.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk test pytest -q tests/test_gateway_console_next_session.py`
- `rtk test pytest -q tests/test_gateway_users.py`
- `rtk npm run build` from `apps/aiops_console_web`

## Review gates

- Browser calls Gateway only.
- No generic env-var editor.
- No secrets or internal URLs in API responses or UI.
- Writes are admin-only, CSRF-protected, versioned, and audited.
- Rollback appends history; it never deletes or rewrites versions.
- Policy visibility does not bypass later action/approval/grant execution.
- No new dependency or service.

## Rollback points

- Backend is additive: remove `/api/settings*`, `/api/policies*`, and stop using
  the new SQLite DB.
- Frontend can fall back to placeholder pages without touching auth/session.
- Policy classifier is read/explain-only in this child, so action execution can
  ignore it until the action slice consumes it.

## Out of scope

- Draft/publish workflow.
- Full policy simulator.
- PostgreSQL/config distribution.
- Secret editing.
- Executing policy grants in action flows.
