# Implementation Plan

## Checklist

1. Load frontend specs:
   - `.trellis/spec/hermes-agent/frontend/index.md`
   - component, state, input/forms, and quality guides.
2. Inventory current static files and fixtures.
3. Review `/tmp/aiops-sre-console.html` and extract only the necessary shell
   structure, visual tokens, and representative content.
4. Create shared Console shell CSS/JS primitives with minimal duplication.
5. Design the full product skeleton navigation before tuning individual panels:
   active MVP entries are usable, non-MVP entries are disabled and marked
   `planned`.
6. Upgrade or add static screens:
   - Overview default route;
   - incident list/work queue as an Overview section;
   - incident detail in the new shell;
   - Approval Center preview panel on Overview.
7. Keep real approval/execution controls disabled unless backed by Gateway
   Approval Service and RBAC; local-only state transitions may be used to review
   UI states.
8. Add or preserve Agent process panels that show tool calls, query/input
   summaries, observation summaries, refs, durations, missing evidence reasons,
   and diagnosis confidence without exposing chain-of-thought.
9. Add skeleton states for Gateway-provided Grafana/fallback metadata and
   diagnostic cost/usage summaries; unavailable data must render as unavailable,
   not zero or blank.
10. Cover panel-level loading, empty, error, unauthorized, partial,
    unavailable, and stale/conflict states where fixture coverage is cheap.
11. Update fixtures only as needed to cover blocked on-call and approver-capable
   approval preview states.
12. Run frontend/static tests.
13. Open the static HTML locally or via a simple server and inspect desktop/mobile
   screenshots before considering the task complete.

## Validation Commands

```bash
pytest -q tests/test_aiops_console_incident_detail.py
```

Add or update static tests for any new HTML/JS files.

## Rollback Points

- Revert new shell files independently if they do not improve the current
  incident detail slice.
- Keep old fixture names until replacement tests prove equivalent coverage.
