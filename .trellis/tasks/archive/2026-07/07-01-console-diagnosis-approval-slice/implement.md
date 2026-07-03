# Implementation Plan

## Checklist

1. Load specs before editing:
   - `.trellis/spec/hermes-agent/backend/index.md`
   - `.trellis/spec/hermes-agent/frontend/index.md`
   - frontend component/state/quality guides
   - backend API/auth/testing/quality guides
2. Inspect current durable incident/writeback shape:
   - `apps/aiops_k8s_gateway/diagnosis_writeback.py`
   - incident store/domain code
   - existing `GET /incidents/{incident_id}` smoke route
   - Console fixtures and tests
3. Add or extend a Gateway `/api/*` diagnosis-process read route with real RBAC.
4. Normalize existing writeback/session data into a frontend-friendly payload.
5. Update `apps/aiops_console/static/incident-detail.*` to render live-backed
   process data while preserving fixture scenarios.
6. Add/adjust tests:
   - Gateway route authz and payload shape;
   - Console no direct non-Gateway calls;
   - static complete/empty/partial/failed scenarios still parse;
   - PodCrashLooping partial-with-real-data-gaps fixture shape.
7. Run focused tests first, then the relevant backend/frontend test files.

## Validation Commands

```bash
pytest -q tests/test_aiops_console_incident_detail.py
pytest -q tests/test_command_gateway_skeleton.py tests/test_gateway_identity_rbac.py
pytest -q tests/test_hermes_diagnosis_service.py tests/test_incident_diagnosis.py
```

Adjust the Gateway test set after inspecting the route's existing test home.

## Rollback Points

- Revert Console live fetch changes if static fixture regression appears.
- Revert the new Gateway route independently if payload normalization is wrong;
  it should not alter existing writeback or webhook behavior.

## Deferred

- Chat/follow-up Agent controls.
- Approval Center.
- Topology/Loki live data gap fixes.
