# Directory Structure

> `apps/aiops_console/` is a Python package marker around a static frontend slice.
> There is no build step, no bundler, no `node_modules`.

```
apps/aiops_console/
  __init__.py                            # package marker ("Static AIOps Console frontend slices.")
  README.md                              # slice contract: Gateway-only API assumptions
  static/
    incident-detail.html                 # semantic HTML shell with aria labels
    incident-detail.js                   # vanilla IIFE render logic
    incident-detail.css                  # one hand-written stylesheet
  fixtures/
    incident-detail-fixtures.js          # window.AIOPS_INCIDENT_FIXTURES = {...} mock data
```

## Rules backed by code

- **One slice = three static files** (`<name>.html` + `<name>.js` + `<name>.css`)
  plus one `fixtures/<name>-fixtures.js`. A new slice copies this shape.
- **Fixtures live under `fixtures/`, not `static/`**, and are loaded by a relative
  script tag before the page logic (`incident-detail.html:153`:
  `<script src="../fixtures/incident-detail-fixtures.js"></script>` then
  `<script src="./incident-detail.js"></script>`). The page is opened directly in a
  browser with no server required.
- **`README.md` documents the API contract** and is asserted by
  `tests/test_aiops_console_incident_detail.py:88` (must mention `Gateway only`,
  `GET /api/incidents/{incident_id}`, "never calls Hermes, Connector, MCP,
  Prometheus, Loki, or Feishu", "Full chain-of-thought is never shown"). Update the
  README whenever the contract changes.
- **ASCII filenames, `incident-detail` kebab-case** for the slice. JS uses
  camelCase (`setScenario`, `replaceChildren`).

## Anti-patterns

- Adding `package.json`, `tsconfig.json`, or a bundler — reviewable-without-Node is an
  explicit design constraint, enforced by tests refusing `fetch(` /
  `XMLHttpRequest`.
- Serving the slice from anywhere but the Gateway. The static set is reviewed as
  files; production serves it through the Gateway only.
