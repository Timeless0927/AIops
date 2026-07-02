# implement.md — live diagnosis entry fix

## Checklist

1. Read backend specs: API routes, authorization, logging/audit, testing,
   quality, cross-layer guide.
2. Add Gateway service-token authorization helper:
   - only activates for `PERMISSION_K8S_READ`
   - returns an `Actor` with k8s-read permission and wildcard namespace scope
   - normal bearer session behavior unchanged
3. Add Hermes K8s-read auth header:
   - read `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN` or `AIOPS_GATEWAY_SERVICE_TOKEN`
   - pass header only to Gateway `/k8s/read`
4. Add/update tests:
   - Gateway `/k8s/read` accepts service token
   - service token cannot access `/api/case-profile` or equivalent user route
   - Hermes `_k8s_read_adapter` sends Authorization when env is set
5. Update deploy docs/examples:
   - add `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS=30` to dev-external config
   - add secret example keys for service token and writeback secret
6. Run checks:
   - `python3 -m pytest -q tests/test_gateway_identity_rbac.py tests/test_hermes_diagnosis_service.py tests/test_diagnosis_provider.py tests/test_diagnosis_llm_tooluse.py`
   - `python3 -m compileall -q apps hermes toolsets aiops`
7. Live validation if image can be built/deployed in this session:
   - set real secret values
   - rollout Gateway/Hermes
   - rerun Alertmanager smoke
   - assert `llm-tooluse-v1`, no k8s 401, writeback succeeded

## Rollback

- Remove new env keys from runtime secret/config.
- Revert code commit; Gateway returns to requiring user bearer tokens only.
- No database migration is involved.
