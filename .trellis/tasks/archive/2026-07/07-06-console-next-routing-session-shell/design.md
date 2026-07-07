# Console Next routing/session shell design

## Scope

This slice builds only the foundation needed by later Console pages:

- real URL routing
- Gateway-served frontend assets
- login `next`
- route guards
- cookie session with Bearer compatibility
- minimal CSRF
- Chinese-first shell with `中文 | EN`

It does not implement full incidents, agent runs, approvals, audit, users, or
settings behavior. Those pages may use placeholders, but their routes and guard
states must exist.

## Decisions

- Use `react-router` for primary routing.
- Do not build a custom router on `window.history`.
- Keep Browser -> Gateway-only API calls.
- Serve production frontend assets from Gateway.
- Use an explicit application route fallback allowlist.
- Keep existing Bearer auth working while adding cookie auth.
- Make the new Console frontend use cookie auth by default.
- Use minimal double-submit CSRF for cookie-authenticated mutating requests.
- Store language preference in `localStorage["aiops.console.locale"]`.
- Default language is `zh-CN`.

## Frontend routing

Primary routes:

- `/login`
- `/incidents`
- `/incidents/:incidentId`
- `/incidents/:incidentId/report`
- `/agent-runs`
- `/agent-runs/new`
- `/agent-runs/:runId`
- `/approvals`
- `/approvals/:approvalId`
- `/audit`
- `/audit/:chainId`
- `/policies`
- `/users`
- `/users/:userId`
- `/settings`
- `/search`
- `/notifications`

Route behavior:

- Unauthenticated protected routes redirect to `/login?next=<current-path>`.
- Login success navigates to `next` when it is an internal path.
- Missing/unsafe `next` falls back to `/incidents`.
- Unauthorized routes render 403.
- Unknown application routes render 404.
- Refreshing a resource route preserves route and resource id.
- Primary navigation state comes from the URL, not `activeView`.

## Gateway asset serving

Gateway serves static frontend files from a configured dist directory.

Recommended configuration:

- `AIOPS_CONSOLE_DIST_DIR`: optional path to built frontend assets.
- If unset or missing, Gateway keeps API behavior and returns API-style 404 for
  application routes.
- Packaged production image sets the dist directory to the built Vite output.

Application fallback allowlist:

- `/`
- `/login`
- `/incidents*`
- `/agent-runs*`
- `/approvals*`
- `/audit*`
- `/policies*`
- `/users*`
- `/settings*`
- `/search*`
- `/notifications*`

Never fallback:

- `/api/*`
- `/auth/*`
- `/healthz`
- `/readyz`
- `/metrics`
- `/connectors*`
- `/webhooks/*`
- `/diagnosis/*`
- `/k8s/*`
- any other Gateway/service endpoint

Static asset behavior:

- Existing files under the dist directory are served directly.
- Missing application routes return `index.html`.
- Unknown non-application paths return API-style 404.
- Content type uses stdlib MIME detection.
- Built Vite assets remain content-hashed.

## Auth and session

Existing session token storage stays as the session authority for this slice.
The change is how the browser carries the session.

Login:

- `POST /auth/login` authenticates as today.
- Response keeps returning the legacy token during compatibility.
- Response also sets an HttpOnly session cookie.
- Cookie uses `SameSite=Lax`.
- Cookie uses `Secure` when Gateway is configured for secure cookies or request
  forwarding indicates HTTPS.

Current user:

- `/auth/me` accepts either Bearer or session cookie.
- Response shape remains compatible.

Authorization:

- Gateway authorization accepts Bearer and cookie sessions.
- Existing Bearer tests continue to pass.
- New frontend requests omit Authorization by default and rely on same-origin
  cookies.

Logout:

- `POST /auth/logout` clears the session cookie.
- Clearing the cookie is enough for browser logout in this slice.
- Server-side token invalidation can be added later if needed.

## CSRF

Use minimal double-submit CSRF:

- `GET /auth/csrf` returns `{ csrf_token }`.
- Frontend stores the token in memory.
- Cookie-authenticated mutating requests send `X-CSRF-Token`.
- Gateway validates the token for cookie-authenticated mutating requests.
- Bearer-token requests may skip CSRF.

This slice proves enforcement on one mutating route. Later slices reuse the same
client request helper for all writes.

## Language switching

Default locale:

- `zh-CN`

Supported locales:

- `zh-CN`
- `en-US`

Storage:

- `localStorage["aiops.console.locale"]`
- no user preference API in this slice

UI:

- Login page shows `中文 | EN`.
- Authenticated shell top bar shows `中文 | EN`.
- Switching language does not navigate, reload, clear filters, clear selected
  resources, or clear unsaved form state.
- Document `lang` should reflect the selected language.

Translation boundary:

- Translate navigation, buttons, labels, form help, empty states, loading text,
  errors, dialogs, and status labels.
- Do not translate logs, commands, IDs, hashes, user input, incident titles, or
  external evidence payloads.

## Shell layout

Desktop:

- Top bar: global search placeholder, environment badge, Gateway status, language
  switch, notifications, user menu.
- Left nav: Operations, Evidence, Governance, Admin groups.
- Main area: page header, action/filter bar, page body, optional context strip.

Mobile:

- Top bar keeps page title, language switch, notifications, user menu.
- Bottom nav covers Incidents, Approvals, Notifications, Me.
- Mobile route support focuses on shell and guard behavior in this slice.

## Guard and placeholder pages

Until later slices implement full pages, protected routes may render placeholder
pages with:

- localized title
- short localized empty/coming-soon state
- route id/resource id when present
- 403/404 support

Placeholders must not call internal services directly.

## Risks

- Cookie auth touches Gateway auth paths used by many tests. Keep Bearer
  compatibility to reduce blast radius.
- Asset fallback can accidentally hide API 404s. Use allowlist fallback only.
- Language switching can reset app state if implemented as reload. Keep locale in
  React state and localStorage.

## Deferred

- Server-side user language preference.
- Full translation catalog extraction tooling.
- Full logout token revocation.
- Complete CSRF coverage for every future write route.
- Final mobile approval UX.
