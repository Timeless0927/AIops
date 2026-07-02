# Authorization

> There is exactly one auth boundary: the Gateway/control-plane. Bearer token in
> -> `SessionTokenStore` -> `Actor` -> `Actor.can(permission, scope)` -> audit row.
> Browser, Hermes, Connector, MCP never bypass it.

---

## The bearer-token flow

`apps/aiops_k8s_gateway/main.py:105`:

```python
def _authorize(handler, permission, scope, request_id) -> Actor | None:
    token = _extract_bearer_token(handler.headers.get("Authorization"))
    session = _SESSIONS.get(token or "")
    if session is None:
        _record_gateway_authz_audit(actor=None, request_id=request_id,
            permission=permission, resource_scope=scope, decision="deny",
            result="unauthorized")
        handler.write_json(HTTPStatus.UNAUTHORIZED,
            _error_payload("unauthorized", "missing or invalid bearer token", request_id))
        return None
    actor = session.actor
    if not actor.can(permission, scope):
        _record_gateway_authz_audit(actor=actor, request_id=request_id,
            permission=permission, resource_scope=scope, decision="deny",
            result="forbidden")
        handler.write_json(HTTPStatus.FORBIDDEN,
            _error_payload("forbidden", f"permission denied: {permission}", request_id))
        return None
    return actor
```

Call-site convention (`apps/aiops_k8s_gateway/main.py:319`):

```python
actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
if actor is None:
    return                       # response already written
```

Rules:
- **`_authorize` returns `None` and has already written the HTTP response** on any
  denial. Always `return` immediately when it returns `None`.
- **Token extraction** (`_extract_bearer_token`, line 66) requires the `Bearer`
  scheme and a non-empty token.
- **Both deny outcomes emit an audit row** (`audit_log.record_audit`) with
  `result` `unauthorized` (no token) or `forbidden` (token but scope/permission
  denied). Never skip the audit on a denial.

---

## RBAC model — `aiops/domain/identity.py`

- **Roles** are a fixed set: `admin`, `user`, `oncall_approver`
  (`identity.py:21-23`). `ROLE_ALIASES` (line 35) normalizes spellings.
- **Permissions** are a fixed set of string constants (`PERMISSION_VIEW_INCIDENT`,
  `PERMISSION_K8S_READ`, `PERMISSION_APPROVE_ACTION`, `PERMISSION_QUERY_AUDIT`,
  `PERMISSION_SYNC_LDAP`, ...). The `ROLE_PERMISSIONS` matrix (line 45) maps each
  role to a `frozenset` of permissions.
- **`Actor.can(permission, scope)`** (line 212) = `permission in self.permissions()`
  AND (`has_role(admin)` OR `scope.matches(resource_scope)`).
- **`Scope`** (line 138) is three tuples: `services`, `teams`, `namespaces`.
  `Scope.matches` (line 155) uses `_scope_dimension_allows` (line 692): empty
  allowed-set means "must be empty requested-set"; `"*"` in allowed matches anything;
  `"*"` in requested never matches a concrete allowed set.
- **`admin` bypasses scope.** `actor.can(...)` returns `True` for any `scope` when
  `ROLE_ADMIN` is present (line 215). Do not add an extra admin short-circuit.

## Building a resource Scope from a payload

Prefer the existing helpers rather than re-deriving from raw dicts:
- `_resource_scope_from_payload(payload)` — reads `service/team/namespace`
  (`apps/aiops_k8s_gateway/main.py:75`).
- `_approval_resource_scope(approval)` — reads approval `resource_scope` with
  `service_id|service`, `team_id|team`, `namespace` fallbacks (line 83).
- `_incident_resource_scope(incident)` — uses `_required_scope_value`, which
  turns missing into the sentinel `"__missing_scope__"` so empty fields do not
  accidentally match a wildcard (line 92, 97).

Build scopes via `aiops.domain.identity.resource_scope(service=, team=, namespace=)`
(`identity.py:633`) — never hand-construct `Scope(services=(...), ...)`.

## Gateway auth modes

1. **User bearer auth** — `_authorize` against `_SESSIONS`, a process-local
   `SessionTokenStore` in `identity.py:334` (in-memory, `secrets.token_urlsafe(32)`,
   8h TTL). Issued by `POST /auth/login` via `IdentityProvider.login`
   (`identity.py:540`), which dispatches to static seed users in the SQLite
   `SQLiteIdentityStore` or to LDAP (`_login_ldap`, line 562).
2. **Hermes K8s-read service token** — Hermes may call Gateway `/k8s/read` with
   `Authorization: Bearer <AIOPS_GATEWAY_SERVICE_TOKEN>` (or Hermes-side override
   `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN`). `_authorize` accepts that token only
   when `permission == PERMISSION_K8S_READ`, returns synthetic actor
   `aiops-hermes` with role `oncall_approver` and wildcard scope, and still runs
   normal `Actor.can(...)` + Gateway audit on the route. The same bearer value
   must not authorize `/api/*`, approval, audit, sync, or incident routes.
3. **Service writeback auth** — Hermes writes diagnosis back to Gateway through a
   shared-secret HMAC, **not** a bearer token. Header
   `X-AIOPS-Writeback-Signature` (env `AIOPS_GATEWAY_WRITEBACK_SECRET`), verified by
   `aiops/contracts/writeback_auth.py` `verify_writeback_signature`. Gateway side
   gatekeeper: `apps/aiops_k8s_gateway/diagnosis_writeback.py:17`
   `authorize_writeback_request`, which **fails closed** (returns 401 if the env
   var is unset or the signature is invalid). See `hermes/service_main.py:236`
   for the producer side.

Anti-patterns:
- A GET/POST handler that performs its action before calling `_authorize`.
- Returning ad-hoc 403 JSON instead of routing through `_authorize` + audit.
- Letting the browser reach Hermes/Connector/MCP directly — the console must go
  through Gateway `/api/*` (see frontend specs / project `CLAUDE.md`).

## Scenario: Hermes service token for Gateway K8s evidence

### 1. Scope / Trigger

- Trigger: Hermes diagnosis needs read-only K8s evidence from Gateway, but no
  browser/user session exists in the service-to-service hop.

### 2. Signatures

- Gateway env: `AIOPS_GATEWAY_SERVICE_TOKEN=<opaque secret>`.
- Hermes env: `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN=<opaque secret>`; if absent,
  reuse `AIOPS_GATEWAY_SERVICE_TOKEN`.
- HTTP request: `POST /k8s/read` with `Authorization: Bearer <token>`.

### 3. Contracts

- The token is optional; if unset, behavior remains the normal user-session
  bearer flow and missing/invalid bearer returns 401.
- The token is accepted only for `PERMISSION_K8S_READ`.
- The synthetic actor is `actor_id=username=aiops-hermes`, role
  `oncall_approver`, wildcard service/team/namespace scope.
- The route must still record success audit with `permission=k8s_read`,
  `decision=allow`, and `actor=aiops-hermes`.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Token matches and route calls `_authorize(..., PERMISSION_K8S_READ, ...)` | allow as `aiops-hermes` |
| Token missing or mismatched on `/k8s/read` | 401 unauthorized via normal bearer/session path |
| Same token used on `/api/case-profile` or other user route | 401 unauthorized, no service-token bypass |
| Gateway env unset while Hermes sends token | 401 unauthorized |

### 5. Good/Base/Bad Cases

- Good: Hermes `_k8s_read_adapter` sends the bearer token only to Gateway
  `/k8s/read`, and Gateway returns audit context for `aiops-hermes`.
- Base: no service token configured; Gateway denies anonymous K8s reads.
- Bad: checking only `Authorization` equality without permission gating would
  turn the service token into a broad browser/API bypass.

### 6. Tests Required

- `tests/test_gateway_identity_rbac.py`: real `ThreadingHTTPServer` test that
  service token succeeds on `/k8s/read` and fails on `/api/case-profile`.
- `tests/test_hermes_diagnosis_service.py`: `_k8s_read_adapter` injects
  `Authorization: Bearer ...`, with Hermes-specific override taking precedence.

### 7. Wrong vs Correct

Wrong:

```python
if _extract_bearer_token(headers.get("Authorization")) == os.getenv("AIOPS_GATEWAY_SERVICE_TOKEN"):
    return _HERMES_SERVICE_ACTOR
```

Correct:

```python
if permission == PERMISSION_K8S_READ and hmac.compare_digest(token, configured):
    return _HERMES_SERVICE_ACTOR if _HERMES_SERVICE_ACTOR.can(permission, scope) else None
```
