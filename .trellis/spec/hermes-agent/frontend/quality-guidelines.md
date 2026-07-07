# Quality Guidelines

> Hard guardrails for Console V1 static slices, enforced by
> `tests/test_aiops_console_incident_detail.py` and the slice `README.md`.

---

## Forbidden (breaks tests or contract)

- `XMLHttpRequest`, browser-direct service URLs, or non-Gateway network calls.
- `fetch(` outside the Gateway adapters. Allowed Console V1 live calls are
  `POST /auth/login`, `GET /api/incidents/active`, and
  `GET /api/incidents/{incident_id}/diagnosis-process`, guarded so direct
  `file://` review and no-session states stay fixture-only.
- `execute` / `mutation` keywords in the HTML (the test lower-cases and checks).
- Any action row rendered with `execution_enabled: true` (the test asserts `False`).
- Reaching Hermes, Connector, MCP, Prometheus, Loki, or Feishu from the browser —
  Gateway `/api/*` only.
- `innerHTML` / template strings that embed data — use `textContent` and the
  `valuePath`/format helpers (component-guidelines.md). (Needed both for XSS safety
  and to keep the surface static.)
- `node_modules`, bundlers, TS — no Node toolchain is required to review the slice.
- Removing the contract lines from `README.md` — the test asserts the required
  strings (`Gateway only`, `GET /api/incidents/{incident_id}`, the never-calls list,
  "Full chain-of-thought is never shown").

## Required

- Each slice = `static/<name>.html` + `static/<name>.js` + `static/<name>.css` +
  `fixtures/<name>-fixtures.js`, opened directly in a browser with no server.
- Shared shell assets are okay when reused by multiple slices, but they must stay
  local-only: no network calls, no `innerHTML`, and no mutation/approval
  side-effects. Page scripts that render fixture data should run before shared
  interaction scripts that query those rendered nodes.
- Default scenario behavior: requested-or-`complete` (`setScenario`, line 35);
  init from `?scenario=` (line 296).
- `aria-label` / `aria-labelledby` on sections, `aria-pressed` on scenario buttons,
  `role="status"` on inline-state containers.
- New fixture scenario must satisfy the same shape contract; if it needs a new
  invariant (e.g. fails differently), add a test in
  `tests/test_aiops_console_incident_detail.py` that asserts it.

## Review checklist

- [ ] No `XHR`/`execute`/`mutation` in JS/HTML; any `fetch` is the single
      Gateway auth/read adapter and all rendering still uses `textContent`/helpers.
- [ ] New action rows keep `execution_enabled: false`.
- [ ] HTML ids added in JS have matching `nodes.X` (component contract).
- [ ] `README.md` still documents the Gateway-only contract; `tests/test_aiops_console_incident_detail.py` still green.
- [ ] No Node toolchain introduced; slice openable from `static/<name>.html`.
- [ ] Any new scenario covered by a fixture + test.

## Console Next React/Vite shell

Console Next lives in `apps/aiops_console_web/` and is allowed to use the
existing React/Vite toolchain. V1 rules above still apply to `apps/aiops_console/`.

### 1. Scope / Trigger

- Trigger: building routes, login, route guards, and shell pages for Console Next.
- Boundary: browser -> same-origin Gateway only. No browser call may target
  Hermes, Connector, MCP, Prometheus, Loki, Feishu, or any internal service URL.

### 2. Signatures

- Frontend build: `npm run build` in `apps/aiops_console_web`.
- Router dependency: `react-router`.
- Gateway auth calls used by the shell:
  - `POST /auth/login`
  - `GET /auth/me`
  - `GET /auth/csrf`
  - `POST /auth/logout`

### 3. Contracts

- Primary navigation state comes from real routes, not `activeView`-style state.
- Production browser auth uses same-origin HttpOnly cookies; the Console Next app
  must not store or send `Authorization: Bearer` for its main session path.
- Mutating cookie-authenticated requests send `X-CSRF-Token`.
- Locale defaults to `zh-CN`, switches between `zh-CN` and `en-US`, and persists in
  `localStorage["aiops.console.locale"]`.
- Switching locale must not navigate, reload, clear selected route params, or clear
  unsaved form state.

### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Unauthenticated protected route | Redirect to `/login?next=<current-path>` |
| Safe internal `next` after login | Navigate back to that route |
| Unsafe or missing `next` | Navigate to `/incidents` |
| User lacks page role | Render 403 in the router shell |
| Unknown Console route | Render 404 in the router shell |
| Cookie mutating request without CSRF | Gateway returns `403 csrf_required` |

### 5. Good/Base/Bad Cases

- Good: `/incidents/<id>` refreshes through Gateway fallback, then React Router
  renders the same route and raw `<id>`.
- Base: placeholder pages are acceptable until later slices own their content.
- Bad: reintroducing sessionStorage bearer tokens or a custom `window.history`
  router for primary navigation.

### 6. Tests Required

- `tests/test_aiops_console_web.py`: dependency, route-shell labels,
  `activeView` absence, cookie/CSRF client calls, and Gateway-only strings.
- `npm run build`: TypeScript and Vite production build.

### 7. Wrong vs Correct

Wrong:

```tsx
sessionStorage.setItem("aiops.console.token", token)
setActiveView("incidents")
```

Correct:

```tsx
await fetch("/auth/login", { credentials: "same-origin", method: "POST" })
<Route path="/incidents/:incidentId" element={<ProtectedRoute />} />
```
