# Console Next routing/session shell implementation plan

## Order

1. Add routing dependency
   - Add `react-router`.
   - Keep existing React/Vite setup.
   - Do not add a state management library.

2. Replace primary in-memory navigation
   - Introduce router routes for login, incidents, agent runs, approvals, audit,
     policies, users, settings, search, and notifications.
   - Remove primary `activeView` routing from the shell.
   - Keep placeholder page bodies where downstream slices own the real feature.

3. Add locale support
   - Add a tiny translation map for shell/login/guard/placeholder text.
   - Store locale in `localStorage["aiops.console.locale"]`.
   - Default to `zh-CN`.
   - Add `中文 | EN` switch to login and top bar.
   - Update document `lang` when locale changes.

4. Add cookie-compatible auth client
   - Make frontend requests same-origin and cookie-friendly.
   - Stop storing/using Bearer token in the new Console path.
   - Keep compatibility in backend and tests for Bearer callers.
   - Implement login `next` redirect behavior.
   - Implement session-expired redirect to `/login?next=<current-path>`.

5. Add Gateway cookie auth compatibility
   - Login sets HttpOnly session cookie and keeps returning legacy token.
   - `/auth/me` accepts Bearer or cookie.
   - Gateway authorization accepts Bearer or cookie.
   - Add logout endpoint that clears cookie.

6. Add CSRF baseline
   - Add `GET /auth/csrf`.
   - Frontend fetches CSRF token and sends `X-CSRF-Token` on mutating cookie
     requests.
   - Enforce CSRF on at least one cookie-authenticated mutating route.
   - Bearer-token requests remain compatible without CSRF.

7. Add Gateway static asset serving
   - Serve dist assets from `AIOPS_CONSOLE_DIST_DIR`.
   - Add application route fallback allowlist.
   - Preserve API-style 404 for Gateway/service endpoints and unknown
     non-application paths.
   - Ensure static assets use correct content type.

8. Update packaging
   - Package Vite build output into the Gateway image.
   - Keep development Vite workflow.
   - Leave old standalone Console Web shape only as compatibility until a later
     cleanup explicitly removes it.

9. Update tests
   - Frontend build/typecheck.
   - Route refresh and placeholder route tests.
   - Login `next`.
   - 403 and 404.
   - Language switch persistence and no route reset.
   - Cookie login/current-user/logout.
   - Bearer compatibility.
   - CSRF issuance/enforcement.
   - Gateway asset fallback allowlist and API 404 preservation.

## Validation commands

- `rtk npm run build` from `apps/aiops_console_web`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk test pytest -q tests/test_gateway_identity_rbac.py`
- Add focused tests as needed for Gateway static assets and CSRF.

## Review gates

- No browser calls to internal services.
- No primary `activeView` route state.
- Existing Bearer auth tests still pass.
- Unknown API/backend paths do not return `index.html`.
- Language switch works without reload.
- Cookie session path works without browser-readable token storage.

## Rollback points

- Router conversion is frontend-only until auth/static serving changes land.
- Cookie auth is additive; Bearer compatibility remains rollback path.
- Gateway static serving is gated by `AIOPS_CONSOLE_DIST_DIR`.
- CSRF enforcement starts with one route before broad rollout.

## Out of scope

- Full page implementations for incidents, runs, approvals, audit, users, and
  settings.
- User profile language preference API.
- Removing standalone Console Web deployment.
- Removing Bearer token compatibility.
- Full write-route CSRF coverage beyond the proof route.
