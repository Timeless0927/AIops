# Console Next routing, Gateway assets, session, and route guard shell

## Parent

Plan AIOps Console Next rebuild.

## What to build

Build the foundational Console shell for real URL routing, Gateway-served
frontend assets, cookie-based production sessions, CSRF protection, and route
guards. This slice replaces primary in-memory view switching with real routes
and makes refresh/deep links preserve the current resource.

The completed slice should be demoable as: unauthenticated user opens a deep
Console route, gets redirected to login with `next`, logs in, returns to the
requested route, and can refresh without losing route/resource context.

## User Stories Covered

- Parent stories 1, 2, 14, 15, 16, 55, 56, and 57.

## Requirements

- Browser calls only Gateway/control-plane API.
- Production serves Console assets from the Gateway Pod and does not expose a
  Node/Vite server.
- Development may still use Vite.
- Application routes return frontend assets, while API routes keep normal API
  behavior.
- Gateway frontend fallback is an explicit application-route allowlist, not a
  catch-all for every unknown path.
- Application routes that may return the frontend entry point include `/`,
  `/login`, `/incidents*`, `/agent-runs*`, `/approvals*`, `/audit*`,
  `/policies*`, `/users*`, `/settings*`, `/search*`, and `/notifications*`.
- Gateway/service endpoints such as `/api/*`, `/auth/*`, `/healthz`, `/readyz`,
  `/metrics`, `/connectors*`, `/webhooks/*`, `/diagnosis/*`, and `/k8s/*` never
  fall back to the frontend entry point.
- Primary routes are real URLs, including login, incidents, agent runs,
  approvals, audit, policies, users, and settings.
- Primary Console routing uses `react-router`; do not build a custom router on
  top of `window.history`.
- Login supports `next` and returns users to the requested route.
- Console is Chinese-first and includes a global `中文 | EN` language switch in
  the shell and login page.
- Language preference is stored locally in the browser using
  `localStorage["aiops.console.locale"]` with values `zh-CN` and `en-US`.
  This slice does not add a user preference API.
- Language switching preserves current route, selected resource, filters, and
  unsaved form state.
- UI labels, buttons, empty states, loading states, errors, dialogs, and
  navigation support Chinese and English. Logs, IDs, hashes, commands, user
  input, incident titles, and external evidence payloads are not automatically
  translated.
- Session expiry returns 401 and the frontend redirects to login with the
  current path.
- Production auth uses HttpOnly same-origin cookies. Bearer tokens may remain
  only for development, tests, or service compatibility.
- Cookie sessions and existing Bearer token sessions run in parallel during
  migration. Production Console uses cookies; existing tests, service
  compatibility, and legacy callers may continue using `Authorization: Bearer`.
- Login sets the HttpOnly session cookie and may keep returning the legacy token
  while compatibility remains enabled. Logout clears the cookie.
- Mutating requests require a Gateway-issued CSRF token.
- CSRF uses a minimal double-submit style flow for cookie-authenticated browser
  requests: frontend fetches a CSRF token from Gateway, sends it as
  `X-CSRF-Token` on mutating requests, and Gateway validates it for cookie
  session writes. Bearer-token service/test requests may skip CSRF.
- This slice must prove CSRF issuance and enforcement on at least one mutating
  route; later slices extend the same client behavior to their write APIs.
- Route guards show 403 for unauthorized routes and 404 for unknown routes.
- Navigation hides pages the current user cannot access.

## Acceptance Criteria

- [ ] Deep-link login preserves `next` and returns to the requested route.
- [ ] Refreshing a resource route keeps the same route and resource.
- [ ] Primary navigation does not rely on in-memory `activeView`-style state.
- [ ] Console dependency list includes `react-router`, and route guards, 403,
  404, and login `next` are implemented through router-level routes/loaders or
  route wrappers rather than a custom history router.
- [ ] Default language is Chinese, with a visible `中文 | EN` switch in the top
  bar and login page.
- [ ] Language preference persists and switching language does not reset the
  current route, selected resource, filters, or unsaved form state.
- [ ] Gateway serves content-hashed frontend assets and falls back to the
  frontend entry point for application routes.
- [ ] API routes are not swallowed by frontend route fallback.
- [ ] Unknown non-application Gateway paths still return API-style 404 instead
  of `index.html`.
- [ ] Cookie login, logout, session expiry, and current-user behavior work from
  the Console shell.
- [ ] Existing Bearer token tests and service compatibility paths continue to
  work while the Console uses cookie auth by default.
- [ ] CSRF token issuance and enforcement are covered for at least one mutating
  route.
- [ ] Unauthorized routes render 403 and unknown routes render 404.
- [ ] Frontend remains Gateway-only and does not call internal services.
- [ ] Build/typecheck and route/session contract tests pass.

## Blocked by

None - can start immediately.

## Further Notes

This is the first blocker for most later UI slices. Keep it minimal: shell,
routes, asset serving, session, CSRF, and route guards only.
