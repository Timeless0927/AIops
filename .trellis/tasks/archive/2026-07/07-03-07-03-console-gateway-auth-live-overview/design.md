# Design

## Data Flow

1. Browser opens `/console/console-overview.html`.
2. If no session token exists, Overview renders fixtures and shows a login panel.
3. Login posts credentials to Gateway `/auth/login`.
4. Gateway returns the existing session bearer token and actor payload.
5. Overview stores the token in `sessionStorage`.
6. Overview calls `GET /api/incidents/active` with `Authorization: Bearer <token>`.
7. Gateway lists active incidents, filters each row with
   `actor.can(PERMISSION_VIEW_INCIDENT, _incident_resource_scope(row))`, and
   returns normalized Overview rows.
8. Clicking a row navigates to `/console/incident-detail.html?incident_id=<id>`.

## Boundaries

- Browser calls Gateway only.
- Gateway owns static file serving, login, RBAC, and active incident filtering.
- No browser-direct Hermes/Connector/MCP/Prometheus/Loki/Feishu calls.
- Static file serving is limited to `apps/aiops_console/static` and
  `apps/aiops_console/fixtures`.

## Compatibility

- `file://` review keeps fixture rendering.
- Existing incident detail live route remains unchanged.
- No database schema changes.

## Minimal Choices

- Use `sessionStorage`, not cookies or refresh tokens.
- Add one Overview live endpoint, not a general incident search API.
- Add small `write_static_file` logic in Gateway, not a separate web server.
