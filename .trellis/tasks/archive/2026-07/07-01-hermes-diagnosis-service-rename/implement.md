# Rename Hermes diagnosis service boundary - implementation plan

## Order

0. Complete `07-06-remove-hermes-agent-dependency`.
   - This removes the vendored agent collision first.
   - Do not start this rename while `hermes-agent/` is still a runtime dependency.

1. Add a tiny runtime naming helper.
   - New env wins, old env fallback.
   - Test invalid/missing env behavior.

2. Update Gateway handoff naming.
   - Accept `AIOPS_DIAGNOSIS_URL` / `AIOPS_DIAGNOSIS_PATH`.
   - Keep old `AIOPS_HERMES_URL` / `AIOPS_HERMES_DIAGNOSIS_PATH`.
   - Rename internal function/event names only where tests prove no audit break.

3. Rename diagnosis service Python boundary.
   - Move `hermes/` to `diagnosis_service/`.
   - Add minimal `hermes` shim imports for compatibility.
   - Update Dockerfile, runtime smoke imports, tests, and direct imports.

4. Rename image and K8S resources.
   - Docker target: `diagnosis`.
   - Image: `timelessmao/aiops-diagnosis`.
   - Deployment/Service: `aiops-diagnosis`.
   - Keep `aiops-hermes` alias Service selecting `aiops-diagnosis`.
   - Do not rename `aiops-hermes-data` PVC in this task.

5. Update docs/spec/ADR.
   - Mark Hermes naming as legacy alias.
   - Update ADR-0003 Future Work to completed.
   - Keep explicit note that `hermes-agent/` is not this service.

6. Deploy path.
   - Push via GitHub CI.
   - Update `aiops-dev` to the new diagnosis image.
   - Verify Gateway handoff, diagnosis writeback, console diagnosis process.

## Checks

Run the smallest useful checks during implementation:

```bash
rtk test pytest -q tests/test_hermes_diagnosis_service.py tests/test_alert_webhook.py tests/test_split_service_packaging.py tests/test_k8s_manifests.py
rtk test pytest -q tests/test_diagnosis_llm_tooluse.py tests/test_diagnosis_provider.py tests/test_incident_diagnosis.py
rtk test pytest -q tests/test_aiops_console_web.py
rtk npm --prefix apps/aiops_console_web run build
```

Before merge/deploy:

```bash
rtk test pytest -q
rtk git status --short --branch
```

K8S verification after image publish:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-diagnosis --timeout=180s
kubectl -n aiops-dev get svc aiops-diagnosis aiops-hermes
kubectl -n aiops-dev logs deploy/aiops-diagnosis --tail=100
```

## Rollback

Use the previous `aiops-hermes` image tag and previous manifests. The old env names and alias Service should keep Gateway handoff working during rollback.

## Do Not Do

- Do not rename `hermes-agent/` or vendored CLI config.
- Do not change diagnosis model/provider behavior.
- Do not remove legacy env names in the same PR.
- Do not rename persistent PVCs in-place.
