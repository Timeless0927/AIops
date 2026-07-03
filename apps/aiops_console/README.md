# AIOps Console Static Slices

This directory contains lightweight frontend vertical slices for Console V1.
They are intentionally static so they can be reviewed without a Node toolchain.

## Incident Detail

Open `static/incident-detail.html` in a browser. The page loads mock data from
`fixtures/incident-detail-fixtures.js` and covers these scenarios:

- `complete`: full Gateway incident view with diagnosis, timeline, evidence,
  action proposal, and audit summary.
- `empty`: incident exists but no diagnosis session or evidence has been
  persisted yet.
- `partial`: diagnosis completed with partial evidence and failed/empty cards.
- `failed`: diagnosis session failed while preserving readable timeline and
  audit context.

## Overview

Open `static/console-overview.html` in a browser for fixture review, or
`/console/` from the Gateway process for live login/session review. The page uses
the shared Console shell and `fixtures/console-overview-fixtures.js` to show the
first product-grade operations workbench:

- complete product skeleton navigation with non-MVP entries marked `planned`;
- active incident work queue inside Overview;
- Agent/tool process visibility without full chain-of-thought;
- approval preview for a blocked on-call user and an approver fixture state;
- honest unavailable/planned states for cost usage and Grafana fallback panels.

## API Assumptions

The slice is built against the AIO-87 Gateway-only contract and the AIO-95
writeback shape:

- Browser reads incident state from Gateway only.
- Gateway serves the Console shell at `/console/` and `/console/console-overview.html`.
- Overview live incident data comes from `GET /api/incidents/active` with a
  Gateway bearer token from `POST /auth/login`.
- The durable diagnosis-process source is
  `GET /api/incidents/{incident_id}/diagnosis-process` in Console V1.
- When `static/incident-detail.html` is opened directly from disk, or no
  `incident_id` query parameter is present, the page stays offline and renders
  `fixtures/incident-detail-fixtures.js`.
- AIO-95 currently exposes a lower-level protected Gateway smoke view at
  `GET /incidents/{incident_id}` with HMAC. A production Console adapter should
  map that durable artifact into the AIO-87 `/api/incidents/{incident_id}/diagnosis-process`
  envelope before browser use.
- The page never calls Hermes, Connector, MCP, Prometheus, Loki, or Feishu.
- Action proposals are read-only; mutation execution controls are out of scope.
- Diagnosis reasoning is summarized only. Full chain-of-thought is never shown.
