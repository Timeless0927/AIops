# Implement

1. Inspect `apps/aiops_k8s_gateway/main.py` approval routes and
   `approval_service.py`.
2. Add the smallest missing Gateway route only if approve/reject is not already
   exposed.
3. Add Approval Center view to React without introducing a router dependency.
4. Test Gateway route behavior and frontend API paths.
5. Validate:

```bash
rtk npm --prefix apps/aiops_console_web run build
rtk test pytest -q tests/test_aiops_console_web.py tests/test_gateway_approval*.py
```

Rollback point: revert approval route additions and frontend approval view.
