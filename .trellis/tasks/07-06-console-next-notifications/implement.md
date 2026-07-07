# Console Next notifications implementation

## Steps

1. Reuse the existing Gateway `notification_center` delivery store.
   - Keep Feishu/external channels notification-only.
   - Keep delivery failure isolated from approval/execution state.
2. Add Console-facing Gateway routes.
   - `GET /api/notifications`
   - `GET /api/notifications/stream`
   - `POST /api/notifications/retry`
   - Scope-filter Console-visible notifications from delivery payload context.
3. Add Console Next notification center route.
   - `/notifications` lists scoped deliveries, retry/dead-letter state, and
     uses finite SSE replay for page-level notification events.
4. Add focused tests.
   - Gateway tests for scope filtering, delivery records, dedupe/retry/dead-letter,
     failure isolation, and notification-only payloads.
   - Console contract tests for same-origin notification calls and EventSource.

## Validation

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/notification_center.py`
- `rtk test pytest -q tests/test_gateway_console_notifications.py`
- `rtk test pytest -q tests/test_gateway_notification_center.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk npm run build`
- `rtk git diff --check`
