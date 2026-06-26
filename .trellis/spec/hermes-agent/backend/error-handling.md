# Error Handling

> Two error shapes coexist: the Gateway's flat `_error_payload` for HTTP routes,
> and the `ToolEnvelope`+`ErrorCode` enum for MCP facades. Service layers raise a
> `*Error(ValueError)`; the HTTP boundary catches it and maps `code`/`status` to a
> controlled JSON response. **Never leak exception text to a browser; never let an
> unhandled exception become a 500 with a stack trace.**

---

## Gateway HTTP error helper — `apps/aiops_k8s_gateway/main.py:57`

```python
def _error_payload(code, message, request_id):
    return {"service": APP_NAME, "status": "failed", "request_id": request_id,
            "error": {"code": code, "message": message}}
```

Status mapping is **case-by-case** at the route, not via a framework:
- `401` → `code: "unauthorized"` (no/invalid bearer — `_authorize`, line 117)
- `403` → `code: "forbidden"` (scope/permission deny — line 129)
- `400` → `code: "invalid_request"` (bad JSON / missing fields — lines 362, 717)
- `404` → `code: "not_found"` (missing approval/incident — lines 377, 821)
- `503` → `code: "connector_offline"` / `#{peer}` unavailable — lines 576, 754

Some legacy/webhook routes use a looser inline shape (`{"service", "status":"invalid",
"error": str(exc)}`) — tolerate it where you find it, but write new routes with
`_error_payload`.

---

## Service errors — `*Error(ValueError)` with `code`/`status`

A service layer raises a controlled error; the route catches a specific
exception type and maps it. Canonical example —
`apps/aiops_k8s_gateway/approval_service.py:80`:

```python
class ApprovalServiceError(ValueError):
    def __init__(self, code, message, *, status=400, approval=None):
        super().__init__(message)
        self.code = code; self.message = message
        self.status = status; self.approval = approval
```

Caught and mapped by the route (`apps/aiops_k8s_gateway/main.py:765`):

```python
try:
    approval, idempotent = approval_service.create_request(payload, actor_id=actor.actor_id, request_id=request_id)
except approval_service.ApprovalServiceError as exc:
    handler.write_json(exc.status, _approval_error_payload(exc, request_id))
    return
```

`_approval_error_payload` (line 993) attaches `approval_request` to the body when
`exc.approval` is set, so a 409 "already decided" carries the conflicting object.

Identity uses the same shape: `IdentityError(ValueError)` with `code`/`message`
(`aiops/domain/identity.py:129`) — codes `invalid_credentials`, `ldap_unavailable`,
`ldap_config_incomplete`, `user_disabled`, etc. The `/auth/login` route maps
`ldap_unavailable` to 503 and others to 401 (`apps/aiops_k8s_gateway/main.py:466`).

### Rules for a new service error

- Subclass `ValueError`; keep fields `code: str`, `message: str`, `status: int``
  (kw-only). Reuse an existing class if the service already has one.
- Carry optional business context (e.g. `approval`) as a kw-only field, surfaced by
  the boundary — not appended to the message string.
- Raise with `... from exc` when chaining off a low-level error
  (`approval_service.py:693`).
- The route catches the **specific** `*Error` class, not bare `Exception`.

---

## MCP facade errors — `ToolEnvelope` + `ErrorCode`

Observability MCP facades always return `HTTPStatus.OK` with a `ToolEnvelope`
(`apps/observability_http.py:100`); the failure is *inside* the envelope, not the
HTTP status:

```python
try:
    envelope = asyncio.run(query_handler(payload))
except Exception as exc:
    envelope = _failure_envelope(tool_name=tool_name, ..., message=str(exc))
self.write_json(HTTPStatus.OK, asdict(envelope))
```

- `ToolEnvelope` fields (`aiops/contracts/envelope.py:12`): `request_id`,
  `tool_name`, `status` (`succeeded`/`failed`/`partial`), `summary`,
  `correlation_id`, `data`, `evidence_refs`, `audit`, `truncated`, `next_cursor`,
  `errors: tuple[ToolError, ...]`.
- `ErrorCode` is a shared string enum (`aiops/contracts/errors.py:10`):
  `INVALID_REQUEST`, `UNAUTHORIZED`, `BACKEND_UNAVAILABLE`, `CONNECTOR_OFFLINE`,
  `APPROVAL_REQUIRED`, `EXECUTION_FAILED`, `TASK_NOT_FOUND`, `TIMEOUT`,
  `OUTPUT_TRUNCATED`, ... `ToolError(code, message, details)` (line 30).
- A failed tool path returns `_failed_tool_envelope` (see
  `hermes/service_main.py:574`): `status="failed"`, `audit.error_code="backend_unavailable"`.

## Common mistakes

- Returning `500`/stack trace for a known domain failure — always map to a
  controlled code. Unknown exceptions may surface only at the facade envelope
  boundary (MCP) or, for Gateway routes, never.
- Putting human-readable details in `error.message` that include a secret
  (writeback secret, token, DB password). Sanitize before sending.
- Mixing the two shapes in one route — a Gateway HTTP route should use
  `_error_payload`; a MCP `/query_*` route should use `ToolEnvelope.errors`.
- Catching `BaseException` — services catch `sqlite3.OperationalError` for lock/busy
  retry internally (see database-guidelines.md) and re-raise; don't swallow.
