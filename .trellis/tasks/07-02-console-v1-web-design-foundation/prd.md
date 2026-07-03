# Console V1 web design foundation

## Goal

Design and implement the first product-grade Console V1 web foundation before
adding more live workflows. The output should make the app feel like a serious
operations console, not a demo page.

## User Value

The operator should immediately understand where they are, what needs attention,
what the agent did, and what actions are blocked by permissions or approval.

## Confirmed Facts

- Existing frontend is vanilla HTML/CSS/JS under `apps/aiops_console/static/`.
- There is no Node, React, or TypeScript toolchain.
- Current `incident-detail` slice has useful data panels but weak product
  framing.
- Frontend specs require Gateway-only access and static fixture coverage.
- SaaS/ops tooling should be quiet, utilitarian, dense, and optimized for
  scanning rather than marketing-style presentation.
- First visual direction is an operations workbench: left navigation, top
  environment/user/time-range context, dense overview, incident list,
  diagnosis detail, and approval preview.
- Default first route is Overview, not the incident list.
- Approval Center appears only as a read-only preview panel on Overview in this
  foundation.
- `/tmp/aiops-sre-console.html` is the open-design reference baseline: a
  three-column SRE workbench with overview/work queue, diagnosis/evidence, and
  approval/tool/audit panels.
- Approval is part of the permission boundary: high-risk commands must show that
  human authorization is required before execution can proceed.
- Default static view shows a normal on-call blocked state (`can_approve=false`);
  an approver-capable state (`can_approve=true`) should be covered as an
  alternate fixture/state.
- Shared shell CSS/JS should be split now so Overview and `incident-detail.html`
  use the same product frame.
- Non-MVP skeleton navigation entries should be disabled and marked `planned`
  instead of linking to placeholder pages.
- Agent diagnosis must not feel like a black box: the UI should expose
  structured tool calls, inputs/queries, observations, results, gaps, latency /
  cost metadata when available, and the summary that led to the diagnosis.
- The design foundation should reserve skeleton space for Gateway-provided
  Grafana/fallback panel metadata and diagnostic cost/usage summaries, even if
  those remain unavailable or planned in the static slice.

## Requirements

1. Establish a Console shell with:
   - left navigation;
   - top operational context bar;
   - workspace title/actions area;
   - responsive content region.
2. Design the complete Console skeleton before live workflow wiring:
   - Overview;
   - Incidents;
   - Diagnosis;
   - Approvals;
   - Notifications;
   - Executions;
   - Audit;
   - Settings.
3. Define a visual system for:
   - severity/status tokens;
   - tool-step state chips;
   - incident cards/rows;
   - timeline rows;
   - evidence panels;
   - Grafana/fallback and cost/usage summary panels;
   - approval/action states;
   - empty, loading, denied, failed, unavailable, and stale/conflict states.
4. Build representative static screens first:
   - overview dashboard as the default first route;
   - incident list/work queue as an Overview section;
   - incident diagnosis detail;
   - approval center preview panel on Overview.
5. Keep UI controls realistic and permission-aware; local static interactions
   may demonstrate filtering, evidence tabs, tool detail expansion, and approval
   state transitions, but any real approve/reject/execute behavior must remain
   Gateway/RBAC-backed.
6. Show Agent process transparency without exposing private chain-of-thought:
   - ordered tool call / evidence collection steps;
   - tool name, status, duration, query/input summary, observation summary, and
     refs;
   - missing/skipped/failed evidence reasons;
   - final diagnosis summary, confidence, and root-cause statement.
7. Preserve existing fixture-driven test style and no-direct-service-call tests.
8. Avoid decorative hero sections, nested cards, oversized marketing layouts,
   gradients/orbs, and one-note color palettes.

## Acceptance Criteria

- Static Console shell opens locally without a build step.
- Shared shell CSS/JS is used by the Overview shell and incident detail page.
- The Overview first viewport clearly communicates AIOps Console, current
  environment, active incidents, blocked approvals, notification state,
  execution state, and primary navigation.
- Primary navigation shows the complete Console skeleton even when non-MVP areas
  are disabled and marked `planned`.
- The incident diagnosis detail screen can represent the latest live smoke shape:
  `partial`, writeback succeeded, many succeeded tool steps, and real topology /
  Loki gaps.
- UI remains readable on desktop and mobile widths.
- Tests continue to prove browser code does not call Hermes/Connector/MCP/
  Prometheus/Loki/Feishu directly.
- Existing fixture scenarios remain available or are migrated to equivalent
  shell scenarios.
- Approval preview covers both blocked on-call and approver-capable UI states
  without enabling real execution.
- Agent/tool process panels make it clear what was called, what came back, what
  was missing, and why the diagnosis is partial/succeeded/failed.
- Cost/usage and Grafana/fallback areas render honest unavailable/planned states
  instead of blank panels or fabricated values.
- Panel-level loading, empty, error, unauthorized, partial, unavailable, and
  stale/conflict states do not blank the whole page.

## Out of Scope

- Real login implementation.
- Live Gateway data fetching.
- Approval decisions.
- Mutation execution.
- Free-form Agent chat.
- Full model chain-of-thought display.
- Custom trace waterfall / prompt diff UI.
- Adding a frontend framework or build tool.

## Open Questions

- None.
