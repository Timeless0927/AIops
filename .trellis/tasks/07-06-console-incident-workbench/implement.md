# Implement

1. Read `apps/aiops_console_web/src/App.tsx` and existing console tests.
2. Rework the current single page into incident workbench sections.
3. Add only the state needed for incident selection, loading, error, and empty
   states.
4. Extend `tests/test_aiops_console_web.py` for Chinese labels and Gateway-only
   API paths.
5. Validate:

```bash
rtk npm --prefix apps/aiops_console_web run build
rtk test pytest -q tests/test_aiops_console_web.py
```

Rollback point: revert `apps/aiops_console_web/src/*` and matching tests.
