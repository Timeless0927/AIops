# State Management

> There is no framework state, no store, no context. The only "state" is **scenario
> selection** — which fixture object is currently rendered. Rendering is a pure
> function of the selected fixture. Patterns from
> `apps/aiops_console/static/incident-detail.js`.

---

## Data source: a global fixtures object

Mock data is a single global assigned by the fixtures script and read defensively
(`incident-detail.js:4`):

```js
const fixtures = window.AIOPS_INCIDENT_FIXTURES || {};
```

Fixture shape (see `apps/aiops_console/fixtures/incident-detail-fixtures.js` and the
README scenario list): each scenario key (`complete`, `empty`, `partial`,
`failed`) holds `{ incident, diagnosis, timeline, evidence, actions, audit }`. The
fixture script body is asserted to be a JSON-compatible single assignment by the
test (`tests/test_aiops_console_incident_detail.py:17` extracts it via
`/^window.AIOPS_INCIDENT_FIXTURES\s*=\s*(\{.*\});\s*$/m`).

When wiring to the real Gateway, the same shape is produced by Gateway
`/api/incidents/{incident_id}` (per `README.md`); the page never invents fields.

## Scenario selection is the only state

```js
function setScenario(name) {
  const data = fixtures[name] || fixtures.complete;
  if (!data) return;
  scenarioButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.scenario === name));
  });
  render(data);
}
```

- Defaulting to `fixtures.complete` when the requested scenario is absent is the
  contract; preserve it (`incident-detail.js:35`).
- Initial scenario comes from the URL query (`?scenario=...`), defaulting to
  `"complete"` (line 296): `setScenario(new URLSearchParams(window.location.search).get("scenario") || "complete")`.
- No form state, no reducers, no event bus. A write-back or mutation page is out of
  scope today (see input-forms.md).

## Pure render

`render(data)` (`incident-detail.js:45`) runs every section renderer against the
same `data` object. Each section treats `data.incident`, `data.diagnosis`, etc. as
read-only inputs and fully rebuilds its nodes (component-guidelines.md). There is no
diffing and no preserved DOM state across renders.

State branches are encoded inside renderers, not in shared state:

```js
if (diagnosis.status === "failed") {
  nodes.diagnosisAlert.textContent = `Diagnosis failed: ${failure.message || "..."}`;
  return;
}
if (diagnosis.status === "partial") { ... return; }
```

## Common mistakes

- Caching multipled rendered nodes in module state and diffing — not the pattern;
  just `replaceChildren()` and rebuild.
- Reading scenario state from a global other than `window.AIOPS_INCIDENT_FIXTURES`
  / `location.search`.
- Adding fields to fixtures that the page renders without a matching Gateway
  contract entry — keep fixtures faithful to `README.md` assumptions.
