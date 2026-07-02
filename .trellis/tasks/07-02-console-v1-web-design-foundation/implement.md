# Implementation Plan

## Checklist

1. Load frontend specs:
   - `.trellis/spec/hermes-agent/frontend/index.md`
   - component, state, input/forms, and quality guides.
2. Inventory current static files and fixtures.
3. Create the shared Console shell CSS/HTML primitives with minimal duplication.
4. Upgrade or add static screens:
   - overview/list shell;
   - incident detail in the new shell;
   - approval center preview.
5. Keep all controls read-only/demo-state unless backed by an existing workflow.
6. Update fixtures only as needed to cover design states.
7. Run frontend/static tests.
8. Open the static HTML locally or via a simple server and inspect desktop/mobile
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
