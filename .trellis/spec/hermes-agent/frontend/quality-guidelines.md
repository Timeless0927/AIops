# Quality Guidelines

> Hard guardrails for Console V1 static slices, enforced by
> `tests/test_aiops_console_incident_detail.py` and the slice `README.md`.

---

## Forbidden (breaks tests or contract)

- `fetch(`, `XMLHttpRequest`, or any network call in the static JS — the slice is
  self-contained and render-only (`test_aiops_console_incident_detail.py:23`).
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
- Default scenario behavior: requested-or-`complete` (`setScenario`, line 35);
  init from `?scenario=` (line 296).
- `aria-label` / `aria-labelledby` on sections, `aria-pressed` on scenario buttons,
  `role="status"` on inline-state containers.
- New fixture scenario must satisfy the same shape contract; if it needs a new
  invariant (e.g. fails differently), add a test in
  `tests/test_aiops_console_incident_detail.py` that asserts it.

## Review checklist

- [ ] No `fetch`/`XHR`/`execute`/`mutation` in JS/HTML; `textContent`/helpers only.
- [ ] New action rows keep `execution_enabled: false`.
- [ ] HTML ids added in JS have matching `nodes.X` (component contract).
- [ ] `README.md` still documents the Gateway-only contract; `tests/test_aiops_console_incident_detail.py` still green.
- [ ] No Node toolchain introduced; slice openable from `static/<name>.html`.
- [ ] Any new scenario covered by a fixture + test.
