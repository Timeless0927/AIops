# Implement

1. Do not start real execution integration until `controlled-mutation-execution`
   has a Gateway contract.
2. Add disabled/read-only execution status to action proposal or approval detail.
3. Wire real lifecycle endpoints only after backend guardrails are merged.
4. Test disabled, unauthorized, and authorized states.
5. Validate:

```bash
rtk npm --prefix apps/aiops_console_web run build
rtk test pytest -q tests/test_aiops_console_web.py tests/test_*execution*.py
```

Rollback point: revert execution status UI and Gateway endpoint wiring.
