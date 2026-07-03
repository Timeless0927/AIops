# Design: Gateway-backed Console diagnosis process view

## Boundaries

- Console stays a static vanilla HTML/JS slice under `apps/aiops_console/`.
- Browser calls Gateway `/api/*` only.
- Gateway owns authentication, RBAC, and scope filtering.
- Hermes remains the diagnosis runner and low-level session exporter.
- Connector/MCP/Prometheus/Loki/Topology remain backend-only evidence sources.

## MVP Shape

Use the existing incident detail page as the first screen. Add a live-backed data
path that maps Gateway incident/writeback data into the same render shape used by
the current fixtures.

The MVP is read-only:

- no chat input;
- no follow-up tool execution;
- no approval decision controls;
- no direct Hermes calls from the browser.

## Data Flow

1. Operator opens Console incident detail.
2. Console calls Gateway `/api/incidents/{incident_id}/diagnosis-process` (exact
   path can be adjusted to match local route style).
3. Gateway authorizes `PERMISSION_VIEW_INCIDENT` for the incident scope.
4. Gateway returns a normalized process payload assembled from durable incident
   writeback data. If durable data is missing but a session id is known, Gateway
   may use the existing protected backend path/server-side Hermes export later;
   the first cut should prefer durable writeback to avoid creating a new service
   dependency.
5. Console renders diagnosis, timeline, evidence/tool steps, missing evidence,
   and markdown summary.

## Payload Contract

Keep the frontend payload boring and close to what the existing static fixture
already renders:

- `incident`: id, title, severity, status, service/source labels, permissions.
- `diagnosis`: status, session id, summary, confidence, root cause, markdown.
- `timeline`: ordered entries with time/title/status/summary/refs.
- `evidence`: cards or tool-step rows with kind/status/summary/query/ref/failure.
- `missing_evidence`: source/tool/reason/status.
- `audit`: writeback status and durable refs.

Do not expose model chain-of-thought. Show tool calls, observations, summaries,
and missing reasons only.

## Compatibility

- Preserve existing static fixtures and tests.
- Keep the current lower-level `GET /incidents/{incident_id}` smoke view intact.
- New browser-facing route should be `/api/*` to match Console rules.
- No database schema change unless existing writeback storage lacks fields needed
  for timeline/missing evidence; prefer mapping existing JSON first.

## Future Follow-Up Agent

After operators can see the process, add constrained follow-up commands as a
separate task:

- rerun diagnosis;
- explain partial;
- re-query recent logs;
- inspect a specific pod.

Those commands should be explicit actions with audit, not free-form chat in the
first version.

## Risks

- Durable Gateway data may not include every Hermes timeline field yet.
- Rendering raw tool payloads could leak noisy or sensitive data; show summaries
  and refs first.
- If the route proxies live Hermes memory only, old sessions disappear on pod
  restart. Durable writeback is the safer first source.
