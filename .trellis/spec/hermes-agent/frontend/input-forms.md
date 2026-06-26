# Input & Forms

> Console V1 is **read-only**. There are **no `<form>` submissions, no fetch, no
> mutation controls** in the current slice. User interaction is limited to
> scenario selection buttons. These specs describe what actually exists and the
> hard read-only guardrails. Patterns from `apps/aiops_console/static/incident-detail.{html,js}`.

---

## What "input" looks like today — scenario buttons

```html
<div class="scenario-switcher" aria-label="Scenario">
  <button class="scenario-button" type="button" data-scenario="complete">Complete</button>
  <button class="scenario-button" type="button" data-scenario="empty">No data</button>
  <button class="scenario-button" type="button" data-scenario="partial">Partial</button>
  <button class="scenario-button" type="button" data-scenario="failed">Failed</button>
</div>
```

These purely re-render the slice against a different fixture (`incident-detail.js:293`):

```js
scenarioButtons.forEach((button) => {
  button.addEventListener("click", () => setScenario(button.dataset.scenario));
});
```

- Use `type="button"` explicitly so buttons never become submit buttons in a future
  `<form>`.
- Keep selection state on the button via `aria-pressed` (toggled in `setScenario`).
- No `disabled`/pending states exist yet; they belong here if a real action ever
  ships.

## Read-only guardrails (hard invariants)

Enforced by `tests/test_aiops_console_incident_detail.py` and the `README.md`
contract:

- **No `fetch(`, no `XMLHttpRequest`, no `execute`/`mutation` keywords** in JS/HTML
  (`test_aiops_console_incident_detail.py:23`: `assert "fetch(" not in js`,
  `assert "XMLHttpRequest" not in js`, `assert "execute" not in html.lower()`,
  `assert "mutation" not in html.lower()`).
- **Action proposals are read-only.** Every `action` row is rendered with
  `execution_enabled: false` (the test asserts each `action["execution_enabled"] is
  False`, line 60), and the JS surface reads only `risk_level`,
  `approval_required`, `approval_id` (`renderActions`, `incident-detail.js:197`).
- **Access limits are displayed, not actionable.** `renderAccess` (`incident-detail.js:68`)
  shows `can_view_raw_evidence` / `can_view_cost` / `can_approve` as
  Allowed/Blocked chips plus a `blocked_reason`. There is no "request access" CTA.
- **Diagnosis reasoning is summarized only.** `incident-detail.js:122`:
  `"Conclusion is summarized only. Full reasoning traces are intentionally hidden."`

## If a future slice needs real input (P0/P1 path), the guardrails say it must:

1. Be served and reached only through Gateway `/api/*` — never Hermes/Connector/MCP.
2. Carry a Gateway-owned approval + RBAC + audit trail behind any mutation
   (project `CLAUDE.md`), not a direct fetch.
3. Add the missing test invariants (failure to add `fetch` keeps the current tests
  green — a future writable slice would have to *replace* that assertion, not
  silently weaken it).

## Common mistakes

- Adding a `<form method="POST">` or a `fetch` to "preview" Gateway data — it breaks
  the slice's self-contained, no-runtime contract and its tests.
- Surfacing an approve/reject button on the action-proposal card — approvals go
  through the Approval Center / Gateway Approval Service API, not the Console V1
  slice.
- Reusing `scenario-button` styling for a mutation CTA — keep scenario buttons
  semantics-free.
