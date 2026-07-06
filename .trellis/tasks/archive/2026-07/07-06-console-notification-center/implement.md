# Implement

1. Inspect Gateway notification routes and payload shapes.
2. Add Notification Center view using existing endpoints.
3. Add empty/error/loading states.
4. Test frontend path usage and any new Gateway read endpoint.
5. Validate:

```bash
rtk npm --prefix apps/aiops_console_web run build
rtk test pytest -q tests/test_aiops_console_web.py tests/test_notification*.py
```

Rollback point: revert frontend notification view and any tiny read endpoint.
