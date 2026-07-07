# Authorization

> There is exactly one auth boundary: the Gateway/control-plane. Bearer token in
> -> `SessionTokenStore` -> `Actor` -> `Actor.can(permission, scope)` -> audit row.
> Browser, Diagnosis, Connector, MCP never bypass it.

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
2. **Diagnosis K8s-read service token** — Diagnosis may call Gateway `/k8s/read` with
   `Authorization: Bearer <AIOPS_GATEWAY_SERVICE_TOKEN>` (or diagnosis-side override
   `AIOPS_DIAGNOSIS_GATEWAY_SERVICE_TOKEN`, falling back to legacy
   `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN`). `_authorize` accepts that token only
   when `permission == PERMISSION_K8S_READ`, returns synthetic actor
   `aiops-diagnosis` with role `oncall_approver` and wildcard scope, and still runs
   normal `Actor.can(...)` + Gateway audit on the route. The same bearer value
   must not authorize `/api/*`, approval, audit, sync, or incident routes.
3. **Alertmanager webhook bearer auth** — automatic Alertmanager routing uses
   `Authorization: Bearer <AIOPS_ALERTMANAGER_WEBHOOK_TOKEN>` on
   `POST /webhooks/alertmanager`. This is route-local webhook authentication,
   not a user session and not `_authorize`; the token is accepted only by
   `apps/aiops_k8s_gateway/alertmanager_webhook.py` before payload processing.
   If unset, the old unsigned dev/manual path and optional HMAC behavior remain.
4. **Service writeback auth** — Diagnosis writes diagnosis back to Gateway through a
   shared-secret HMAC, **not** a bearer token. Header
   `X-AIOPS-Writeback-Signature` (env `AIOPS_GATEWAY_WRITEBACK_SECRET`), verified by
   `aiops/contracts/writeback_auth.py` `verify_writeback_signature`. Gateway side
   gatekeeper: `apps/aiops_k8s_gateway/diagnosis_writeback.py:17`
   `authorize_writeback_request`, which **fails closed** (returns 401 if the env
   var is unset or the signature is invalid). See `diagnosis_service/service_main.py`
   for the producer side.
5. **Console Next cookie auth** — browser login still issues the legacy bearer token
   in JSON for compatibility, and also sets an HttpOnly same-origin
   `aiops_session` cookie. `_authorize` accepts either a valid bearer token or a
   valid session cookie. Browser cookie writes must send `X-CSRF-Token`; bearer
   service/test callers skip CSRF.

Anti-patterns:
- A GET/POST handler that performs its action before calling `_authorize`.
- Returning ad-hoc 403 JSON instead of routing through `_authorize` + audit.
- Letting the browser reach Diagnosis/Connector/MCP directly — the console must go
  through Gateway `/api/*` (see frontend specs / project `CLAUDE.md`).

## Scenario: Diagnosis service token for Gateway K8s evidence

### 1. Scope / Trigger

- Trigger: Diagnosis needs read-only K8s evidence from Gateway, but no
  browser/user session exists in the service-to-service hop.

### 2. Signatures

- Gateway env: `AIOPS_GATEWAY_SERVICE_TOKEN=<opaque secret>`.
- Diagnosis env: `AIOPS_DIAGNOSIS_GATEWAY_SERVICE_TOKEN=<opaque secret>`; if absent,
  legacy `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN` is accepted for one migration
  window; if both are absent, reuse `AIOPS_GATEWAY_SERVICE_TOKEN`.
- HTTP request: `POST /k8s/read` with `Authorization: Bearer <token>`.

### 3. Contracts

- The token is optional; if unset, behavior remains the normal user-session
  bearer flow and missing/invalid bearer returns 401.
- The token is accepted only for `PERMISSION_K8S_READ`.
- The synthetic actor is `actor_id=username=aiops-diagnosis`, role
  `oncall_approver`, wildcard service/team/namespace scope.
- The route must still record success audit with `permission=k8s_read`,
  `decision=allow`, and `actor=aiops-diagnosis`.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Token matches and route calls `_authorize(..., PERMISSION_K8S_READ, ...)` | allow as `aiops-diagnosis` |
| Token missing or mismatched on `/k8s/read` | 401 unauthorized via normal bearer/session path |
| Same token used on `/api/case-profile` or other user route | 401 unauthorized, no service-token bypass |
| Gateway env unset while Diagnosis sends token | 401 unauthorized |

### 5. Good/Base/Bad Cases

- Good: Diagnosis `_k8s_read_adapter` sends the bearer token only to Gateway
  `/k8s/read`, and Gateway returns audit context for `aiops-diagnosis`.
- Base: no service token configured; Gateway denies anonymous K8s reads.
- Bad: checking only `Authorization` equality without permission gating would
  turn the service token into a broad browser/API bypass.

### 6. Tests Required

- `tests/test_gateway_identity_rbac.py`: real `ThreadingHTTPServer` test that
  service token succeeds on `/k8s/read` and fails on `/api/case-profile`.
- `tests/test_diagnosis_service.py`: `_k8s_read_adapter` injects
  `Authorization: Bearer ...`, with diagnosis-specific override taking precedence.

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

## Scenario: Alertmanager bearer token for automatic webhook routing

### 1. Scope / Trigger

- Trigger: kube-prometheus-stack Alertmanager should automatically send alerts
  to Gateway `/webhooks/alertmanager`, but Alertmanager generic webhook configs
  cannot compute a body-bound `X-Signature` HMAC per request.

### 2. Signatures

- Gateway env: `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN=<opaque secret>`.
- AlertmanagerConfig receiver:

```yaml
webhookConfigs:
  - url: http://aiops-gateway.aiops-dev.svc.cluster.local:8080/webhooks/alertmanager
    httpConfig:
      authorization:
        type: Bearer
        credentials:
          name: aiops-alertmanager-webhook
          key: token
```

- HTTP request: `POST /webhooks/alertmanager` with
  `Authorization: Bearer <token>` and an Alertmanager JSON body.

### 3. Contracts

- If `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is set, missing or mismatched bearer
  token returns `401` before JSON parsing or incident creation.
- If `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is unset, preserve the existing
  unsigned/manual dev path; optional `ALERTMANAGER_WEBHOOK_SECRET` /
  `AIOPS_ALERTMANAGER_WEBHOOK_SECRET` HMAC still protects callers that can sign
  the body.
- If both bearer token and HMAC secret are set, bearer auth is the automatic
  Alertmanager route contract. Do not require HMAC for Alertmanager because
  Alertmanager cannot produce body HMAC.
- The bearer token is route-local to `/webhooks/alertmanager`; it must not
  authorize `/api/*`, `/k8s/read`, `/diagnosis/writeback`, incident view,
  approval, notification, or audit routes.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Gateway token unset, HMAC unset | unsigned manual/dev webhook still works |
| Gateway token set, bearer missing | 401 `alertmanager bearer token verification failed` |
| Gateway token set, bearer mismatched or non-Bearer scheme | 401 controlled error |
| Gateway token set, bearer matches | process payload, create/reuse incident, trigger Hermes |
| Bearer token reused on any non-webhook route | no effect; route follows its own auth contract |

### 5. Good/Base/Bad Cases

- Good: AlertmanagerConfig reads the token from a Secret in the monitoring
  namespace and posts directly to Gateway with `Authorization: Bearer ...`.
- Base: manual smoke includes the same bearer token when Gateway token auth is
  enabled.
- Bad: adding a signing relay without NetworkPolicy/mTLS. Any pod that can call
  the relay would get a signed Gateway request, which only moves the trust
  boundary instead of enforcing it.

### 6. Tests Required

- `tests/test_gateway_alertmanager_webhook.py`: missing/bad/basic auth fail when
  `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is set; correct bearer token passes; HMAC
  tests remain passing.
- Deployment dry-run: `kubectl apply --dry-run=client -f
  deploy/k8s/alertmanager/aiops-alertmanager-route.yaml -f
  deploy/k8s/alertmanager/alertmanager-webhook-token.example.yaml`.

### 7. Wrong vs Correct

Wrong:

```python
# Treat the Alertmanager token like a global Gateway bearer session.
if _extract_bearer_token(self.headers.get("Authorization")) == os.getenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN"):
    actor = ADMIN_ACTOR
```

Correct:

```python
# Route-local webhook gate before payload processing.
if route_path == "/webhooks/alertmanager":
    status, payload = handle_http_request(body, dict(self.headers))
```

Inside the webhook module, compare only for this route and return 401 before
JSON parsing when the token is configured but invalid.

## Scenario: Console Next cookie session and CSRF

### 1. Scope / Trigger

- Trigger: Console Next production browser sessions use HttpOnly cookies while
  existing tests and service callers keep bearer compatibility.
- Boundary: browser -> Gateway auth routes -> `_SESSIONS` -> `_authorize`.

### 2. Signatures

- `POST /auth/login` returns the existing token response and sets
  `Set-Cookie: aiops_session=<token>; HttpOnly; SameSite=Lax; Path=/`.
- `GET /auth/me` accepts either `Authorization: Bearer <token>` or the
  `aiops_session` cookie.
- `GET /auth/csrf` returns `{"csrf_token": "<server-derived-token>"}` for a valid
  bearer or cookie session.
- `POST /auth/logout` clears the cookie and revokes the current session token when
  one is present.
- Cookie-authenticated mutating requests send `X-CSRF-Token`.

### 3. Contracts

- Bearer wins when it maps to a valid session; otherwise Gateway may fall back to
  a valid session cookie.
- Bearer callers do not need CSRF.
- Cookie-authenticated `POST`/write paths that route through `_authorize` fail
  closed with `403 csrf_required` when `X-CSRF-Token` is absent or wrong.
- The CSRF token is derived server-side from the session token; the frontend never
  reads the session token cookie.
- Cookie `Secure` is enabled when `AIOPS_SECURE_SESSION_COOKIE=true` or
  `X-Forwarded-Proto: https`.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing bearer and missing cookie | `401 unauthorized` |
| Valid bearer | Authorized exactly as before |
| Valid cookie on `GET /auth/me` | `200` current user payload |
| Cookie write without `X-CSRF-Token` | `403 csrf_required` |
| Cookie write with valid `X-CSRF-Token` | Route continues to normal authz |
| Logout with valid cookie + CSRF | Session revoked and cookie cleared |

### 5. Good/Base/Bad Cases

- Good: Console login sets HttpOnly cookie, `/auth/me` works without a browser
  readable token, and logout clears the cookie.
- Base: old bearer-token tests keep passing unchanged.
- Bad: storing the cookie token in `localStorage` or requiring CSRF for bearer
  service/test requests.

### 6. Tests Required

- `tests/test_gateway_console_next_session.py`: login `Set-Cookie`, cookie
  `/auth/me`, bearer compatibility, CSRF issuance/enforcement, logout clearing.
- Existing Gateway identity/RBAC and approval tests must keep passing.

### 7. Wrong vs Correct

Wrong:

```python
session = _SESSIONS.get(_extract_bearer_token(headers.get("Authorization")) or "")
```

Correct:

```python
session, auth_mode = _request_session(handler)
if auth_mode == "cookie" and handler.command == "POST":
    require_csrf(handler, session.token)
```
