# Implement

1. Inspect `toolsets/audit_log.py` and Gateway audit usage.
2. Prefer rendering existing diagnosis-process timeline before adding APIs.
3. Add one authorized read route only if needed.
4. Add frontend audit/history view.
5. Validate:

```bash
rtk npm --prefix apps/aiops_console_web run build
rtk test pytest -q tests/test_aiops_console_web.py tests/test_*audit*.py
```

Rollback point: revert frontend audit view and optional read route.
