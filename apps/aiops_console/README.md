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

## API Assumptions

The slice is built against the AIO-87 Gateway-only contract and the AIO-95
writeback shape:

- Browser reads incident state from Gateway only.
- The durable detail source is `GET /api/incidents/{incident_id}` in Console V1.
- AIO-95 currently exposes a lower-level protected Gateway smoke view at
  `GET /incidents/{incident_id}` with HMAC. A production Console adapter should
  map that durable artifact into the AIO-87 `/api/incidents/{incident_id}`
  envelope before browser use.
- The page never calls Hermes, Connector, MCP, Prometheus, Loki, or Feishu.
- Action proposals are read-only; mutation execution controls are out of scope.
- Diagnosis reasoning is summarized only. Full chain-of-thought is never shown.
