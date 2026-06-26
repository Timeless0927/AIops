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

## Two kinds of auth on the Gateway

1. **User bearer auth** — `_authorize` against `_SESSIONS`, a process-local
   `SessionTokenStore` in `identity.py:334` (in-memory, `secrets.token_urlsafe(32)`,
   8h TTL). Issued by `POST /auth/login` via `IdentityProvider.login`
   (`identity.py:540`), which dispatches to static seed users in the SQLite
   `SQLiteIdentityStore` or to LDAP (`_login_ldap`, line 562).
2. **Service writeback auth** — Hermes writes diagnosis back to Gateway through a
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
