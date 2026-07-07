# Console Next reports and feedback implementation

## Steps

1. Add a small Gateway-owned report/feedback store.
   - Store versioned report HTML and structured feedback in SQLite.
   - Draft generation reads existing incident/workbench/audit data and preserves
     unknowns instead of inventing evidence.
2. Add Gateway routes.
   - `GET /api/incidents/{incident_id}/report`
   - `POST /api/incidents/{incident_id}/report/draft`
   - `POST /api/incidents/{incident_id}/report/publish`
   - `POST /api/feedback`
   - `GET /api/agent-runs/{run_id}/feedback`
3. Add Console Next report route.
   - `/incidents/:incidentId/report` shows draft/published versions, printable
     HTML/export affordance, and feedback form.
4. Add focused tests.
   - Gateway HTTP tests for draft, publish immutability, versioning, HTML export,
     authenticated access, unknown preservation, and feedback persistence.
   - Console contract tests for same-origin report/feedback calls.

## Validation

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/report_service.py`
- `rtk test pytest -q tests/test_gateway_reports_feedback.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk npm run build`
- `rtk git diff --check`
