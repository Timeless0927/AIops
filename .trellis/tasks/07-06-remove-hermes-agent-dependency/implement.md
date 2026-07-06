# Remove hermes-agent runtime dependency - implementation plan

## Order

1. Map current references.
   - `hermes-agent`
   - `hermes_cli`
   - `runtime.hermes_gateway`
   - tests that assert the submodule exists

2. Remove packaging dependency.
   - Drop `-e ./hermes-agent[...]` from `requirements.txt`.
   - Remove `hermes-runtime` stage from `Dockerfile.aiops`.
   - Make `hermes`, `hermes-smoke`, and `aiops` stages inherit from `base`.

3. Remove obsolete runtime entrypoints.
   - Delete or neuter `runtime/hermes_gateway.py`.
   - Delete or neuter `hermes/__main__.py`.
   - Update `deploy/entrypoint.sh` if it still starts `runtime.hermes_gateway`.

4. Remove submodule.
   - `git rm hermes-agent`
   - Remove `.gitmodules` entry.
   - Ensure `.git/config` cleanup is not required for committed state.

5. Update tests.
   - Replace assertions that `hermes-agent` is installed with assertions that it
     is not required.
   - Keep config compatibility tests for `HERMES_HOME` / `HERMES_CONFIG` unless
     the code path is deleted.

6. Update docs.
   - ADR-0003 deletion status.
   - K8S/Docker docs that mention the vendored agent dependency.

## Checks

```bash
rtk test pytest -q tests/test_split_service_packaging.py tests/test_deploy_entrypoint.py tests/test_hermes_entry.py
rtk test pytest -q tests/test_hermes_diagnosis_service.py tests/test_alert_webhook.py tests/test_k8s_manifests.py
rtk test pytest -q tests/test_feishu_approval_overlay.py tests/test_approval_execution_worker.py
```

Before finishing:

```bash
rtk test pytest -q
rtk git status --short --branch
```

## Do Not Do

- Do not rename `hermes/` to `diagnosis_service/`.
- Do not rename K8S `aiops-hermes`.
- Do not change diagnosis/provider behavior.
- Do not add replacement abstractions for the old agent.
