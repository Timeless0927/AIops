# Gateway-backed Console diagnosis process view

## Goal

Move the Console from static fixture-only review toward a Gateway-backed
read-only slice where an authenticated operator can inspect how Hermes reached a
diagnosis for a real incident.

The MVP answers: "诊断完成闭环后,我在哪里看过程?" It does not try to make
Hermes conversational yet.

## Confirmed Facts

- `docs/aiops-console-v1-contract.md` defines the Console V1 contract.
- Current static slice in `apps/aiops_console/static/incident-detail.html` uses
  fixture data and intentionally does not fetch directly from services.
- Browser access must stay Gateway-only; it must not reach Hermes, Connector,
  MCP, Prometheus, Loki, or Feishu approval APIs directly.
- Hermes already exposes low-level in-memory session exports:
  `/diagnosis/sessions/{session_id}`, `/timeline`, `/diagnosis`, and
  `/markdown`.
- Hermes writes diagnosis artifacts back to Gateway; the latest live
  PodCrashLooping smoke completed with writeback succeeded and proved operators
  need a better surface for the timeline and missing evidence.
- `apps/aiops_console/README.md` says production Console should read the durable
  incident detail through Gateway, not from Hermes directly.
- The static incident detail page already has panels for diagnosis, timeline,
  evidence, action proposal, and audit summary, so the narrow MVP can extend
  that page instead of adding a new UI shell.

## Requirements

1. Implement the smallest Gateway-backed read-only incident diagnosis process
   view.
2. Console browser code must call only Gateway `/api/*` endpoints.
3. Gateway must enforce RBAC/scope before returning diagnosis process data.
4. The view must show:
   - incident identity and source labels;
   - diagnosis status, summary, confidence, and session id;
   - ordered Hermes/tool timeline;
   - per-tool status for K8s, Loki, metrics, and topology steps;
   - missing evidence reasons, including `partial` and `failed` tool results;
   - final markdown diagnosis when present.
5. Empty, queued/running, partial, failed, and diagnosed states must render
   explicitly without fabricating evidence refs.
6. Static fixture coverage must remain available for offline UI regression.
7. Existing Gateway/Hermes writeback contracts must stay compatible.

## Acceptance Criteria

- A protected Gateway API returns an incident diagnosis process payload suitable
  for the Console detail page.
- The browser reads diagnosis process data only from Gateway `/api/*`; tests keep
  rejecting direct Hermes/Connector/MCP/Prometheus/Loki/Feishu calls.
- RBAC-denied incident/process data is not rendered to unauthorized users.
- The Console renders a real writeback session shape with timeline steps and
  missing evidence reasons.
- Existing complete/empty/partial/failed fixture scenarios still pass.
- A focused test proves the PodCrashLooping smoke shape can be represented:
  `partial`, writeback succeeded, many succeeded tool steps, and remaining
  topology/Loki data gaps.

## Out of Scope

- Full product-grade dashboard.
- Free-form conversational Agent chat.
- Follow-up tool execution from the browser.
- Mutation execution.
- Approval Center implementation.
- Feishu approval workflow changes.
- Langfuse iframe or browser-direct observability integration.

## Open Questions

- Should the first live-backed page fetch a single incident by URL/id only, or
  also include an incident list/search entry point?
