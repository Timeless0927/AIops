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
