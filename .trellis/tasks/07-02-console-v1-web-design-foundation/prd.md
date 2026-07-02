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

## Requirements

1. Establish a Console shell with:
   - left navigation;
   - top operational context bar;
   - workspace title/actions area;
   - responsive content region.
2. Define a visual system for:
   - severity/status tokens;
   - tool-step state chips;
   - incident cards/rows;
   - timeline rows;
   - evidence panels;
   - approval/action states;
   - empty, loading, denied, and failed states.
3. Build representative static screens first:
   - incident overview/list;
   - incident diagnosis detail;
   - approval center preview.
4. Keep UI controls realistic but read-only unless the workflow already exists.
5. Preserve existing fixture-driven test style and no-direct-service-call tests.
6. Avoid decorative hero sections, nested cards, oversized marketing layouts,
   gradients/orbs, and one-note color palettes.

## Acceptance Criteria

- Static Console shell opens locally without a build step.
- The first viewport clearly communicates AIOps Console, current environment,
  active incidents, and primary navigation.
- The incident diagnosis detail screen can represent the latest live smoke shape:
  `partial`, writeback succeeded, many succeeded tool steps, and real topology /
  Loki gaps.
- UI remains readable on desktop and mobile widths.
- Tests continue to prove browser code does not call Hermes/Connector/MCP/
  Prometheus/Loki/Feishu directly.
- Existing fixture scenarios remain available or are migrated to equivalent
  shell scenarios.

## Out of Scope

- Real login implementation.
- Live Gateway data fetching.
- Approval decisions.
- Mutation execution.
- Free-form Agent chat.
- Adding a frontend framework or build tool.

## Open Questions

- Which visual direction should the design foundation target first?
