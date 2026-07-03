# Implementation Plan

## Checklist

1. Add Gateway static Console serving for `/console/`.
2. Add `GET /api/incidents/active` with real bearer RBAC filtering.
3. Normalize active incidents into Overview row fields.
4. Update Overview HTML/JS/CSS for login, session state, live load, and click-through.
5. Update README/spec/test contracts for allowed Gateway fetches.
6. Add Gateway route tests and static frontend contract tests.
7. Start Gateway locally for browser testing.

## Validation

```bash
pytest -q tests/test_aiops_console_incident_detail.py tests/test_gateway_identity_rbac.py
python3 -m py_compile apps/aiops_k8s_gateway/main.py
```

## Rollback

- Revert Overview live fetch and Gateway `/api/incidents/active` route together.
- Static fixtures still render if live path is unavailable.
