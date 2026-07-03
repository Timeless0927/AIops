# Console Gateway auth and live incident overview

## Goal

Let an operator open the Gateway-hosted Console, log in with an existing Gateway
identity, see live active incidents on Overview, and click through to the
Gateway-backed incident detail page.

## Requirements

1. Serve Console static files from Gateway under `/console/`.
2. Add a protected Gateway read endpoint for active incidents suitable for the
   Overview work queue.
3. Add a minimal login panel to Overview using existing `POST /auth/login`.
4. Store the bearer token in browser session storage for this local Console
   session.
5. Overview fetches active incidents only through Gateway `/api/*`.
6. Clicking an incident opens `incident-detail.html?incident_id=<id>`.
7. Direct file review and no-session states keep fixture fallback.
8. Do not add a framework, bundler, new dependency, or approval/mutation controls.

## Acceptance Criteria

- `/console/` and `/console/console-overview.html` return the Overview HTML from
  the Gateway process.
- `GET /api/incidents/active` requires a user bearer token and filters by
  `PERMISSION_VIEW_INCIDENT` scope.
- Overview login succeeds against `/auth/login`, stores token in session storage,
  loads live incidents, and renders fixture data when unauthenticated/offline.
- Incident rows link to `/console/incident-detail.html?incident_id=<id>`.
- Static tests reject XHR, browser-direct service calls, and non-Gateway fetches.
- Focused Gateway/auth/frontend tests pass.

## Out of Scope

- SSO, cookie sessions, refresh tokens, logout revocation.
- Approval Center implementation.
- Mutation execution.
- Full incident history/search.
