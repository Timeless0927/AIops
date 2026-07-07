# API Routes

> How HTTP routes are defined and routed across the control plane. Pattern is
> uniform: subclass `apps.service_http.JsonHandler` and dispatch in `do_GET` /
> `do_POST` with `if route_path == ...` branches. There is **no** router/decorator
> framework.

---

## The shared handler base — `apps/service_http.py:33`

```python
class JsonHandler(BaseHTTPRequestHandler):
    server_version = "aiops-service-smoke/1.0"

    def log_message(self, format, *args):  # noqa: A003
        return                              # suppress default access logs

    def read_json_body(self) -> JSON:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def write_json(self, status, payload):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def write_not_found(self):
        self.write_json(404, {"status": "not_found", "path": self.path})
```

Conventions enforced by this base:
- **All responses are compact JSON** (`sort_keys=True`, no spaces) with
  `Content-Type: application/json`. Never hand-roll `send_response`.
- **Access logs are suppressed** (`log_message` returns `None`). Do not re-enable
  logging here — durable observability is `audit_log` (see logging-guidelines.md).
- **`read_json_body` rejects non-object bodies** with `ValueError`. Catch
  `(TypeError, ValueError)` at call sites.

---

## Route dispatch pattern

Routes are a flat `if route_path == ...` ladder inside `do_GET` / `do_POST`.
Parse the path once at the top (`apps/aiops_k8s_gateway/main.py:208`):

```python
def do_GET(self):  # noqa: N802
    parsed = urlparse(self.path)
    route_path = parsed.path
    query = parse_qs(parsed.query)
    ...
    self.write_not_found()   # fallthrough default
```

- **GET vs POST split by intent:** reads/listings/healthz/readyz/connectivity are
  `do_GET`; mutations, login, queries with a body, webhooks are `do_POST`
  (`apps/aiops_k8s_gateway/main.py:401`).
- **Parameterized paths are parsed by helper**, not by a router. See
  `_approval_detail_id` (/api/approval-requests/{id}) and `_approval_action`
  (/api/approval-requests/{id}/{approve|reject|cancel|expire}) in
  `apps/aiops_k8s_gateway/main.py:728,738`, and `_parse_incident_view_route`
  (`/incidents/{id}`) at line 683.
- **Query parsing** uses module-level helpers `_first_query_value`,
  `_query_limit` (clamped 1..500, default 100), `_query_offset`, `_query_float`
  (`apps/aiops_k8s_gateway/main.py:690-725`). Reuse these; do not re-parse inline.
- **Dual public paths**: notification/approval endpoints accept both legacy and
  `/api/`-prefixed paths via a set check: `if route_path in {"/notifications/types",
  "/api/notifications/types"}` (`apps/aiops_k8s_gateway/main.py:302`).

## Health & connectivity (every service)

Every service exposes the same two probes, in this order (`apps/aiops_k8s_gateway/main.py:229`):
- `GET /healthz` -> `{"service": APP_NAME, "status": "ok", ...env hints}`.
- `GET /readyz` -> `{"service": APP_NAME, "status": "ok", ...peer counts}`.
- `GET /connectivity/<peer>` -> `connectivity_payload(service=, peer_name=, peer_url=)`
  from `apps/service_http.py:63`, which GETs the peer `/healthz` and returns 503 on
  `OSError/TimeoutError/URLError/ValueError`.

---

## Response envelope shape

Every success response carries `"service": APP_NAME` and `"status": "ok"`, plus a
`request_id`. The standard error helper (`apps/aiops_k8s_gateway/main.py:57`):

```python
def _error_payload(code, message, request_id):
    return {"service": APP_NAME, "status": "failed", "request_id": request_id,
            "error": {"code": code, "message": message}}
```

- **`request_id`** comes from `X-Request-ID` / `X-Correlation-ID` headers, falling
  back to `req-{uuid4().hex}` (`apps/aiops_k8s_gateway/main.py:52`). Echo it in
  every response that has one.
- **MCP facades** use a different envelope: `ToolEnvelope` dataclass serialized
  via `asdict(envelope)` (`apps/observability_http.py:100`). See error-handling.md.

## Adding a new route — checklist

1. Pick `do_GET` (read) or `do_POST` (body-bearing). Add the `if route_path ==` branch.
2. If auth is required, call `_authorize(...)` *before* doing the work (see
   authorization.md); `_authorize` writes its own response on denial, so `return`
   immediately after a `None` actor.
3. Compute `request_id`, build the response with `_error_payload`/`_success`-style
   `{"service": APP_NAME, "status": "ok", "request_id": ...}`.
4. Record an audit row for any state-changing or authorization-relevant action (see
   logging-guidelines.md).
5. Add a test using `ThreadingHTTPServer` + `urllib` (see testing.md).

## Scenario: Controlled Mutation Execution

### 1. Scope / Trigger

- Trigger: Gateway may execute a Kubernetes mutation only after an internal
  approval request is approved and the connector is explicitly opened for the
  test window.
- Boundary: browser/operator -> Gateway approval API -> Gateway execution store
  -> Connector `/commands/execute`; Hermes recommendations never execute
  automatically.

### 2. Signatures

- `POST /api/approval-requests/{approval_id}/execute`
- Auth: bearer session with `PERMISSION_EXECUTE_MUTATION` and the approval
  resource scope.
- Required body fields: `idempotency_key`, `cluster_id`, `namespace`, `argv`,
  `preflight_argv`, `post_check_argv`.
- Connector env: `AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION=true` is required
  for `action_type="mutation"`; default ConfigMap value is `false`.

### 3. Contracts

- Approval must be a Gateway-owned `approved` request; rejected, pending,
  cancelled, or expired approvals fail closed.
- `idempotency_key` plus approval id creates one durable
  `approval_executions` row. Same key/body replays the row; different key/body
  for the same approval is `duplicate_execution`.
- Gateway dispatch order is fixed: preflight read command -> mutation command
  -> post-check read command. Mutation is not attempted when preflight fails.
- Connector mutation allowlist is intentionally narrow: `kubectl rollout
  restart deployment/<name>` and `kubectl scale deployment/<name>
  --replicas=N`, scoped to the envelope namespace.
- Default profiles stay read-only; remediation RBAC and connector env are both
  required for a controlled mutation test.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing/invalid bearer | `401` via `_authorize`, audit deny |
| Bearer lacks `execute_mutation` or approval scope | `403` via `_authorize`, audit deny |
| Approval not approved or expired | `409`, no connector dispatch |
| Execution namespace differs from approval scope | `403 out_of_scope`, no connector dispatch |
| Duplicate approval execution with different key/body | `409 duplicate_execution` |
| Preflight command fails | `preflight_failed`, no mutation dispatch |
| Post-check command fails | execution `rollback_required`, incident status/timeline updated, `execution_result` notification |
| Connector mutation env is false | connector returns `command_rejected` |

### 5. Good/Base/Bad Cases

- Good: approved low-risk deployment restart in a scoped namespace runs
  preflight, mutation, post-check, then records audit/timeline success.
- Base: default bundled/external/disabled profiles can still run `/k8s/read` but
  cannot execute mutation.
- Bad: applying remediation RBAC alone without the Gateway approval and connector
  env gate still does not create a mutation execution path.

### 6. Tests Required

- `tests/test_gateway_approval_service.py`: real Gateway + Connector HTTP route
  test for approved execution, idempotent replay, pending/rejected/expired/
  duplicate/out-of-scope failures, audit/timeline rows, and rollback-required
  post-check failure.
- `tests/test_command_gateway_skeleton.py`: connector mutation env gate and
  allowlist rejection cases.
- `tests/test_k8s_manifests.py`: default ConfigMap keeps mutation disabled and
  remediation RBAC is opt-in/scoped.

### 7. Wrong vs Correct

Wrong:

```python
result = dispatch_read_envelope(build_mutation_envelope(payload), route=route, connector_url=url)
```

Correct:

```python
approval = approval_service.get_request(approval_id)
actor = _authorize(handler, PERMISSION_EXECUTE_MUTATION, _approval_resource_scope(approval), request_id)
execution, _ = approval_execution_service.create_or_replay(approval, payload, actor_id=actor.actor_id)
```

## Scenario: Console diagnosis-process view

### 1. Scope / Trigger

- Trigger: Console V1 needs a browser-facing read-only incident diagnosis process
  view backed by durable Gateway writeback data.
- Boundary: browser -> Gateway `/api/*` -> `incident_store` incident/timeline/evidence
  and `diagnosis_trace`; browser never calls Hermes, Connector, MCP, Prometheus,
  Loki, or Feishu directly.

### 2. Signatures

- `GET /api/incidents/{incident_id}/diagnosis-process`
- Auth: user bearer session only, authorized with `PERMISSION_VIEW_INCIDENT` and
  `_incident_resource_scope(incident)`.
- Response: Gateway envelope with `{"service", "status":"ok", "request_id",
  "ok":true, "process": {...}}`.

### 3. Contracts

`process` is normalized for the static Console renderer:

- `incident`: identity, source labels, service owner labels, `latest_session_id`,
  and display-only permission flags.
- `diagnosis`: status, summary, session id, normalized root cause, confidence,
  markdown, and redaction flags. It must not include model chain-of-thought.
- `timeline`: ordered incident events plus diagnosis tool trace rows with
  summaries, refs, and durations when available.
- `evidence`: successful/partial durable evidence plus synthetic missing rows for
  failed or empty tool results.
- `missing_evidence`: durable missing evidence reasons from Gateway writeback.
- `actions`: read-only recommended actions with `execution_enabled:false`.
- `audit`: durable refs and count summary.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing/invalid bearer token | `401` via `_authorize`, audit result `unauthorized` |
| Bearer lacks incident service/team/namespace scope | `403` via `_authorize`, audit result `forbidden` |
| Incident id not found | `404` `not_found` |
| Incident has no diagnosis | `200` with `diagnosis:null`, empty evidence/trace where absent |
| Writeback status is `partial` or `needs_human` | Console status remains `partial`, never fabricated as success |

### 5. Good/Base/Bad Cases

- Good: PodCrashLooping writeback returns partial diagnosis, K8s evidence, Loki and
  topology gaps, and trace rows for tool calls.
- Base: direct file review of the static page uses fixtures and never calls the
  route.
- Bad: exposing `/incidents/{id}` HMAC writeback smoke view to the browser, or
  making the page call Hermes session export directly.

### 6. Tests Required

- `tests/test_gateway_identity_rbac.py`: real `ThreadingHTTPServer` route test
  with login token, unauthorized request, forbidden scope, normalized payload,
  missing evidence, trace row, and `execution_enabled:false`.
- `tests/test_aiops_console_incident_detail.py`: static contract allows only the
  Gateway `/api/incidents/{incident_id}/diagnosis-process` fetch and still blocks
  XHR/direct service calls.

### 7. Wrong vs Correct

Wrong:

```python
status, payload = asyncio.run(read_incident_view(incident_id))
self.write_json(status, payload)  # HMAC smoke shape, no user RBAC
```

Correct:

```python
incident = asyncio.run(incident_store.get_incident(incident_id))
scope = _incident_resource_scope(incident)
actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
if actor is None:
    return
status, payload = asyncio.run(read_diagnosis_process_view(incident_id))
```

## Scenario: Gateway-hosted Console Overview

### 1. Scope / Trigger

- Trigger: local operators need one Gateway URL to log in and inspect active
  incidents without a Node/dev server.
- Boundary: Gateway serves static Console assets under `/console/`; browser live
  data still uses Gateway auth/read endpoints only.

### 2. Signatures

- `GET /console/` -> `apps/aiops_console/static/console-overview.html`
- `GET /console/<static-file>` -> static Console file
- `GET /fixtures/<fixture-file>` -> static fixture file for relative script paths
- `GET /api/incidents/active` -> active incident rows filtered by
  `PERMISSION_VIEW_INCIDENT`

### 3. Contracts

- Static serving is read-only and constrained to `apps/aiops_console/static` plus
  `apps/aiops_console/fixtures`.
- `GET /api/incidents/active` requires a user bearer token and returns
  `{"service","status":"ok","request_id","incidents":[...]}`.
- Each returned incident row has `incident_id`, `title`, `severity`, `status`,
  `service`, `impact`, `age`, and `tags` for the Overview renderer.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| `/console/` | returns Overview HTML |
| Static path escapes Console roots | `404` |
| Missing/invalid bearer on `/api/incidents/active` | `401` |
| Bearer lacks incident scope | row is filtered out |

### 5. Good/Base/Bad Cases

- Good: `alice` sees only active incidents in her service/team/namespace scope.
- Base: opening `static/console-overview.html` directly still renders fixtures.
- Bad: adding a second web server or serving arbitrary repository files.

### 6. Tests Required

- `tests/test_gateway_identity_rbac.py`: real HTTP server returns `/console/`
  HTML and filters `/api/incidents/active` by scope.
- `tests/test_aiops_console_incident_detail.py`: Overview JS allows only
  `/auth/login` and `/api/incidents/active` fetches.

### 7. Wrong vs Correct

Wrong:

```python
handler.send_response(200)
handler.wfile.write(Path(route_path).read_bytes())  # arbitrary file read
```

Correct:

```python
path = (root / relative).resolve()
path.relative_to(root.resolve())
```

## Scenario: Gateway-hosted Console Next app fallback

### 1. Scope / Trigger

- Trigger: production serves the React/Vite Console Next build from the Gateway Pod
  instead of exposing a Node/Vite server.
- Boundary: Gateway static files from `AIOPS_CONSOLE_DIST_DIR`; API and service
  routes keep JSON behavior.

### 2. Signatures

- Env: `AIOPS_CONSOLE_DIST_DIR=/path/to/dist` enables static serving.
- Static file: `GET /assets/<content-hashed-file>` returns the file when it exists
  under the dist directory.
- App fallback routes:
  - `/`
  - `/login`
  - `/incidents*`
  - `/agent-runs*`
  - `/approvals*`
  - `/audit*`
  - `/policies*`
  - `/users*`
  - `/settings*`
  - `/search*`
  - `/notifications*`

### 3. Contracts

- If `AIOPS_CONSOLE_DIST_DIR` is unset or missing, Gateway keeps API-only behavior.
- Existing files under the dist directory are served directly with stdlib MIME
  type detection.
- Missing allowed application routes return `index.html`.
- API/service routes never fall back to `index.html`: `/api/*`, `/auth/*`,
  `/healthz`, `/readyz`, `/metrics`, `/connectors*`, `/webhooks/*`,
  `/diagnosis/*`, and `/k8s/*`.
- Unknown non-application paths still return `JsonHandler.write_not_found()`.
- Resolved static paths must stay inside the configured dist root.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Dist env unset | App routes return normal JSON 404 |
| Existing `/assets/index-*.js` | `200` JavaScript content type |
| `/incidents/<id>` with dist configured | `200` `index.html` |
| `/api/does-not-exist` | JSON 404, not `index.html` |
| `/not-a-console-route` | JSON 404, not `index.html` |
| Path escapes dist root | Not served |

### 5. Good/Base/Bad Cases

- Good: refreshing `/incidents/demo` loads `index.html`, then React Router owns the
  route.
- Base: Gateway can still run without built assets in API-only tests.
- Bad: `try_files`-style catch-all that returns `index.html` for `/api/*` or
  `/connectors*`.

### 6. Tests Required

- `tests/test_gateway_console_next_session.py`: app fallback, content-hashed asset,
  API 404 preservation, and unknown non-app 404.
- `tests/test_aiops_console_web.py`: Dockerfile packages the Vite dist into the
  Gateway image and sets `AIOPS_CONSOLE_DIST_DIR`.

### 7. Wrong vs Correct

Wrong:

```python
if index.exists():
    write_file(index)  # catches /api/typo too
```

Correct:

```python
if path in app_allowlist:
    write_file(dist / "index.html")
else:
    self.write_not_found()
```

## Scenario: Console Next settings and policy management

### 1. Scope / Trigger

- Trigger: Console Next needs versioned non-secret operational settings and a
  policy explanation surface without exposing service internals to the browser.
- Boundary: browser -> Gateway `/api/settings*` / `/api/policies*` -> Gateway-owned
  SQLite settings store. Browser never calls backing services directly.

### 2. Signatures

- `GET /api/settings` requires `PERMISSION_VIEW_SETTINGS`.
- `POST /api/settings/preview` requires `PERMISSION_MANAGE_SETTINGS`.
- `POST /api/settings` requires `PERMISSION_MANAGE_SETTINGS`.
- `POST /api/settings/rollback` requires `PERMISSION_MANAGE_SETTINGS`.
- `GET /api/policies` requires `PERMISSION_VIEW_POLICY`.
- `POST /api/policies/test` requires `PERMISSION_VIEW_POLICY`.
- DB tables:
  - `settings_versions(version_id, version_number, settings_json, diff_json,
    created_by, created_at, change_summary, critical_confirmed, reload_required)`
  - `policy_hits(id, when_ts, actor, action_type, cluster, namespace, service,
    team, environment, decision, reason, settings_version)`

### 3. Contracts

- `GET /api/settings` returns the current sanitized `settings_version`; the first
  read lazily seeds version 1 from built-in defaults.
- Settings payloads are product settings only: `clusters`, `approval_policy`,
  `action_allowlist`, `notifications`, `security`, and `feature_flags`.
- Secret-like keys are rejected and must not be returned: secrets, tokens,
  passwords, LDAP bind passwords, internal URLs, and database paths.
- `POST /api/settings/preview` returns normalized settings, top-level diff,
  `critical`, `confirmation_text`, and `reload_required`; it does not persist.
- `POST /api/settings` appends a version. Critical diffs require the exact
  confirmation text from preview.
- `POST /api/settings/rollback` appends a new version based on the previous
  version; it never deletes or rewrites history.
- `GET /api/policies` returns active policy explanation, cluster environments,
  action allowlist, and recent policy hits.
- `POST /api/policies/test` classifies one action and records a policy hit.
  Unconfigured clusters resolve to `prod`.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing/invalid session | `401 unauthorized` via `_authorize` |
| Auditor reads settings/policies | `200` for `GET /api/settings` and `GET /api/policies` |
| Auditor previews/saves/rolls back settings | `403 forbidden` |
| Cookie-authenticated write without CSRF | `403 csrf_required` |
| Payload includes secret-like key | `400 secret_field_forbidden` |
| Critical save without exact confirmation | `409 confirmation_required` |
| Rollback when no previous version exists | `409 rollback_unavailable` |
| Unknown cluster in policy test | `environment="prod"` and prod policy decision |

### 5. Good/Base/Bad Cases

- Good: admin previews a cluster/approval-policy change, enters the exact
  confirmation text, saves version 2, sees an audit row, tests an unknown cluster
  as production, then rolls back to append version 3.
- Base: auditor can inspect current settings and policy hits but cannot mutate
  settings.
- Bad: adding a generic env-var editor or returning LDAP bind passwords,
  internal service URLs, token values, or database paths in settings responses.

### 6. Tests Required

- `tests/test_gateway_settings_policy.py`: real Gateway HTTP route test for seed
  read, admin save, rollback, audit rows, auditor write denial, secret-key
  rejection, critical confirmation, unconfigured cluster production fallback,
  and recent policy-hit visibility.
- `tests/test_aiops_console_web.py`: Console Next same-origin calls for
  `/api/settings*` and `/api/policies*`, `manage_settings` / read capabilities,
  CSRF writes, Chinese labels, and no internal service URLs.

### 7. Wrong vs Correct

Wrong:

```python
handler.write_json(200, {"settings": os.environ})  # leaks env/secrets and skips RBAC
```

Correct:

```python
actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
if actor is None:
    return
preview = settings_service.preview(payload)
```

## Scenario: Console Next evidence query

### 1. Scope / Trigger

- Trigger: Console Next needs a Gateway-owned evidence API that can query
  configured observability backends without exposing backend URLs, tokens, or raw
  service routes to the browser or agent.
- Boundary: browser/agent -> Gateway `/api/evidence*` -> `evidence_service`.
  OpenObserve is optional and always called server-side.

### 2. Signatures

- `POST /api/evidence/query` requires `PERMISSION_VIEW_EVIDENCE`.
- `POST /api/evidence/agent-query` requires `PERMISSION_VIEW_EVIDENCE` and an
  allowlisted template.
- Optional env:
  - `AIOPS_OPENOBSERVE_URL`
  - `AIOPS_OPENOBSERVE_TOKEN`
  - `AIOPS_OPENOBSERVE_ORG` (default `default`)

### 3. Contracts

- Request body includes `template`, optional `query_type`, optional
  `advanced_query`, `limit`, `timeout_seconds`, `time_range`, and `scope`.
- Scope fields are `cluster`, `namespace`, `service`, `team`, and optional
  `environment`; `cluster_id`, `service_id`, and `team_id` aliases are accepted.
- Non-admin callers must provide complete cluster/namespace/service/team scope
  and pass `_authorize` for that resource scope.
- Advanced query text is only allowed for admin and auditor roles; viewers,
  operators, and approvers must use templates.
- Agent route templates are limited to `service_overview`, `error_logs`,
  `trace_latency`, `k8s_state`, and `topology_dependencies`.
- Response envelope:
  `{"service","status":"ok","request_id","evidence":{"status","query","scope","limits","sources","redaction"}}`.
- `limits.limit` is clamped to `1..100`, time range to six hours, and timeout to
  five seconds.
- Redaction is mandatory for secret/token/password/authorization/API-key fields
  and values. Kubernetes `{"kind":"Secret"}` `data` and `stringData` are never
  returned.
- Missing/unconfigured OpenObserve returns degraded source panels, not a blank or
  direct browser fallback.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing/invalid session | `401 unauthorized` via `_authorize` |
| Caller lacks `view_evidence` or resource scope | `403 forbidden` via `_authorize` |
| Non-admin passes incomplete scope | fail closed before backend query |
| Operator/viewer/approver sends `advanced_query` | `403 advanced_query_forbidden` |
| Unknown template | `400 template_unknown` |
| Agent route uses non-allowlisted template | `403 template_forbidden` |
| OpenObserve env missing or backend unavailable | `200` with partial/degraded sources |

### 5. Good/Base/Bad Cases

- Good: scoped operator queries `service_overview` for their service and receives
  limited, redacted metrics/logs/traces plus compatibility panel statuses.
- Base: admin/auditor may run an advanced query, still with Gateway scope,
  limits, redaction, and audit.
- Bad: browser code calls OpenObserve, Prometheus, Loki, MCP, or Connector
  endpoints directly, or returns bearer tokens/Secret data in samples.

### 6. Tests Required

- `tests/test_gateway_evidence_openobserve.py`: real Gateway HTTP tests for
  scope authorization, clamped limits, OpenObserve server-side request shape,
  redaction, audit rows, advanced-query policy, agent template policy, and
  degraded unconfigured backend behavior.
- `tests/test_aiops_console_web.py`: Console Next uses only
  `/api/evidence/query`, includes `view_evidence`, Chinese-first labels, CSRF
  writes, and no direct internal service URLs.

### 7. Wrong vs Correct

Wrong:

```typescript
fetch(`${openobserveUrl}/api/default/_search`, { headers: { Authorization: token } })
```

Correct:

```python
actor = _authorize(handler, PERMISSION_VIEW_EVIDENCE, scope, request_id)
evidence = evidence_service.query_evidence(payload, actor=actor, request_id=request_id)
```
