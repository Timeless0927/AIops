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
