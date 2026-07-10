# Workbench reads one Incident-scoped snapshot

Status: accepted

`GET /api/v1/incidents/{incident_id}/workbench` returns the bounded product state needed for the first Workbench render: Incident summary, resource context, Alert Signals, Investigation summary, Evidence Steps, current judgment, Recommended Actions, responsibility, and actor capabilities, together with a snapshot revision and Investigation Event cursor. Gateway projects this DTO from durable state instead of exposing generic panels or internal service records. Unbounded event history is paginated separately and live changes use cursor-replayable SSE, avoiding both an ever-growing snapshot and client-side assembly of many independently timed reads.
