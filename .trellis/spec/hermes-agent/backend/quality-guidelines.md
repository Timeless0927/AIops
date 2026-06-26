# Quality Guidelines

> Concrete forbidden/required patterns and the review checklist for this
> control plane. Frames the rules carried in the other backend specs.

---

## Forbidden patterns

- **Browser reaches a non-Gateway service.** The console must only call Gateway
  `/api/*`; it must never call Hermes, Connector, MCP, Prometheus, Loki, or Feishu
  approval APIs directly (project `CLAUDE.md`; enforced by
  `tests/test_aiops_console_incident_detail.py` which asserts `fetch(` and
  `XMLHttpRequest` are absent from the static JS).
- **A service importing another service's internals.** Use the HTTP contract or
  an `aiops/contracts/` envelope. Enforced by `tests/test_architecture_boundaries.py`.
- **New domain logic in `hooks/` or `runtime/`.** Those are V1 legacy compatibility
  layers; new work goes in `apps/` (services/routes) or `aiops/` (domain/contracts).
- **A Gateway route that performs its action before `_authorize`.** Authorization
  must be checked first; on `None` actor the response is already written, so
  `return` immediately. See authorization.md.
- **Silent denial.** Every deny path must write a controlled JSON error *and*
  an `audit_log` row with `result` `unauthorized`/`forbidden` (see logging).
- **`logging` as the primary record for a request.** The durable record is
  `audit_log` and `incident_events`; `logging` is only a defensive fallback for
  failure paths without a durable store (see logging-guidelines.md).
- **`print()` in a service.** Only smoke CLIs may print.
- **`DROP TABLE`/`DROP COLUMN` against a running store.** Migrations are additive
  `ALTER ADD COLUMN` only (see database-guidelines.md).
- **Hand-rolling `send_response` JSON.** Use `JsonHandler.write_json`
  (`apps/service_http.py`). It is how compact-sorted JSON + Content-Type is produced.
- **Leaking secrets in error messages.** Never put the writeback secret, bearer
  token, or LDAP password into `error.message` or audit rows.

## Required patterns

- **`from __future__ import annotations`** at the top of every backend module —
  every service/domain/store file uses it. Enables `X | None` syntax on 3.10ish.
- **Stable JSON**: `json.dumps(value, ensure_ascii=False, sort_keys=True)` for any
  persisted/hashed payload (`incident_store._json_dumps`, `hermes._stable_digest`).
- **`request_id`** in every Gateway response *and* every `audit_log`/timeline row.
- **WAL connection setup + `_execute_write` retry block** for any new SQLite store
  (database-guidelines.md).
- **ErrorCode/`ToolError` inside a `ToolEnvelope`** for MCP facade failures, HTTP
  200 on the wire (error-handling.md).
- **Audit + timeline on success and denial** for any state-changing or
  authorization-relevant Gateway action.

## Testing requirements

- New route or authz change → a `ThreadingHTTPServer` + `urllib` end-to-end test
  (testing.md, strategy A).
- New domain/store behavior → a pure unit test pointed at `tmp_path` (strategy B).
- Boundary/allow-list → `pytest.mark.parametrize` over the full matrix.
- `pytest tests/` must pass; there is no separate lint gate in the project that the
  specs rely on, but match the `# noqa: N802 / A003 / S603` annotations already
  used on `do_GET`/`do_POST`/`log_message`/`subprocess.Popen` call sites.

## Review checklist

- [ ] Route picks GET vs POST correctly; falls through to `write_not_found`.
- [ ] `_authorize(...)` called before any work; `return` after `None`.
- [ ] Success and deny paths both write `audit_log` (and timeline where applicable).
- [ ] Errors go through `_error_payload` (Gateway) or `ToolEnvelope.errors` (MCP),
  never a raw 500.
- [ ] New store reuses the WAL connection block and `_execute_write` retry.
- [ ] No cross-service import; new shared type is in `aiops/contracts` or `aiops/domain`.
- [ ] `request_id` threaded through response + audit/timeline.
- [ ] Secrets are not in messages, audit rows, or stdout.
- [ ] Test resets `_ROUTES`/`_SESSIONS` and points stores at `tmp_path`.
