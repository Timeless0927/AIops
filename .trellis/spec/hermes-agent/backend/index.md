# Backend Development Guidelines

> AIOps split-service control plane. These specs are **source-backed** — every
> rule points at a real file or repeated local pattern in `apps/`, `aiops/`,
> `hermes/`, `toolsets/`. No template placeholders.

The backend is a set of small Python services (Gateway, Hermes, Connector, MCP
facades) talking over JSON HTTP and SQLite. There is **no** shared web framework
and **no** background job queue — every service is a `http.server.ThreadingHTTPServer`
built on `apps/service_http.JsonHandler`.

---

## Guidelines Index

| Guide | What it covers |
|-------|----------------|
| [Directory Structure](./directory-structure.md) | `apps/` · `aiops/` · `hermes/` · `toolsets/` boundaries |
| [API Routes](./api-routes.md) | `JsonHandler` routing, route helpers, response envelope shape |
| [Authorization](./authorization.md) | bearer token -> `Actor.can(permission, scope)` -> audit record |
| [Error Handling](./error-handling.md) | `ErrorCode` enum, service `*Error(ValueError)`, `_error_payload` |
| [Diagnosis Session Status](./diagnosis-session-status.md) | `_derive_session_status` 推导契约;单路后端不可达降级 partial/needs_human,不一票否决 failed |
| [Logging & Audit](./logging-guidelines.md) | `audit_log` SQLite is the durable channel; stdlib `logging` only as defensive fallback; access logs suppressed |
| [Database Guidelines](./database-guidelines.md) | SQLite WAL, `_execute_write` retry, `ALTER ADD COLUMN` migration guard |
| [Testing](./testing.md) | pytest + `ThreadingHTTPServer` + `urllib` + `conftest.py` async runner |
| [Quality Guidelines](./quality-guidelines.md) | forbidden patterns, boundary rules, review checklist |

**Scope note**: these specs cover the *current* Gateway/Hermes/Connector/MCP
codebase. Legacy `hooks/` and `runtime/` are V1-migration compatibility layers —
new domain logic does **not** land there (see project `CLAUDE.md`).

---

**Language**: human-facing docs are 中文-first per project convention; code
identifiers, paths, commands, and API fields stay English.
