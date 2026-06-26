# Component Rendering

> No framework. "Components" are **render functions** that take plain data and
> rebuild a DOM node with `document.createElement`, then attach it with
> `replaceChildren()` / `appendChild()`. Patterns below are lifted verbatim from
> `apps/aiops_console/static/incident-detail.js`.

---

## The IIFE + cached nodes pattern

The whole slice is one IIFE that caches every mutable DOM node up front by id
(`incident-detail.js:7`), then mutates them:

```js
(function () {
  "use strict";
  const fixtures = window.AIOPS_INCIDENT_FIXTURES || {};
  const nodes = {
    title: document.getElementById("incident-title"),
    incidentId: document.getElementById("incident-id"),
    severity: document.getElementById("incident-severity"),
    ...
    timelineList: document.getElementById("timeline-list"),
  };
  ...
})();
```

- Every `<strong id="...">`, `<span id="...">`, list container, etc. has a matching
  `nodes.X` entry. The HTML ids are the contract between HTML and JS.
- `render(data)` fans out into per-section renderers (`renderIncident`,
  `renderDiagnosis`, `renderTimeline`, `renderEvidence`, `renderActions`,
  `renderAudit`, `incident-detail.js:45`).
- Never use `innerHTML`; never use template literals into HTML. Text always goes
  through `element.textContent = ...` (XSS-safe and matches the slice).

## Rebuilding a list

Replace the whole list each render, then append (`renderTimeline`, line 125):

```js
function renderTimeline(timeline) {
  nodes.timelineList.replaceChildren();
  if (!timeline.length) {
    nodes.timelineList.appendChild(emptyState("No timeline events are available."));
    return;
  }
  timeline.forEach((event) => {
    const item = document.createElement("li");
    item.className = "timeline-item";
    const time = document.createElement("div");
    time.className = "timeline-time";
    time.textContent = formatTime(event.occurred_at);
    ...
    item.append(time, body);
    nodes.timelineList.appendChild(item);
  });
}
```

- **`replaceChildren()` to clear**, `appendChild()` to add. This is the consistent
  idiom (`renderEvidence` line 155, `renderActions` line 197, `renderAudit` line
  225).
- **Class names on elements**, never inline styles; CSS pairs plain classes with
  stateful class names (`status-pill <status>`).
- Compose with `el.append(childA, childB, ...)` (line `item.append(time, body)`).

## Small reusable builders

Factored helpers that return DOM nodes (keep these tiny and pure):

```js
function statusPill(status) {
  const span = document.createElement("span");
  setStatus(span, status || "neutral");
  return span;
}
function setStatus(node, status) {
  node.className = `status-pill ${status || "neutral"}`;
  node.textContent = status || "unknown";
}
function emptyState(text) {
  const box = document.createElement("div");
  box.className = "empty-state";
  box.textContent = text;
  return box;
}
function refChip(value) {
  const span = document.createElement("span");
  span.className = "ref-chip";
  span.textContent = value || "-";
  return span;
}
```

- `setStatus` is the only place that writes the `status-pill <status>` class combo —
  reuse it; do not re-stringify class names inline.
- `emptyState` is the single empty-card/empty-list builder; every list render uses
  it for the no-data branch.

## Safe value access

Nested/optional fields go through `valuePath`, never through bare chaining:

```js
function valuePath(object, path) {
  return path.split(".").reduce((value, key) => (
    value && value[key] !== undefined ? value[key] : null), object);
}
```

Used as `valuePath(incident, "service.service_name")` (line 60). Display formatters
guard their input: `formatConfidence` returns `"-"` for non-numbers (line 275),
`formatTime` returns the raw value if not a parseable date (line 282).

## Accessibility

- The HTML shell uses `aria-label` on sections and `aria-labelledby` to bind headings
  to panels (`incident-detail.html:11,44,108`). Preserve aria wiring when you add a
  panel.
- `role="status"` on inline-state containers (`incident-detail.html:67,105`).

## Common mistakes

- `innerHTML = ...` — never. It would also break the no-`fetch` /
  self-contained test invariants.
- Updating a cached `nodes.X` by re-querying inside a renderer — query once in the
  IIFE, mutate the same node.
- Pretty-printing via template strings with raw data — go through `textContent` and
  the format helpers.
