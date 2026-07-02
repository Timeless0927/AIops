# Implementation Plan

## Checklist

1. Load backend specs before editing:
   - `.trellis/spec/hermes-agent/backend/index.md`
   - relevant backend guides for API boundaries, diagnosis status, testing, and
     quality.
2. Add or tighten manifest tests for `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS=30` in
   `dev-external`.
3. Add tests around `_build_tool_args_from_llm` for unsafe/invalid LLM args:
   - k8s argv with wrong namespace, unsafe subcommand, all-namespaces, or shell
     style command.
   - Loki excessive `time_range` and `max_lines`.
   - topology service inference when only namespace is present vs when service,
     app, workload, or pod fields are present.
   Keep fixtures centered on PodCrashLooping in `dev-external/demo-apps`.
4. Implement per-tool normalization in `toolsets/incident_diagnosis.py` with
   small helpers and no broad refactor.
5. If needed, adjust Hermes incident extraction so Alertmanager target fields are
   available to tool arg construction.
6. Update `deploy/k8s/README.md` with a drift check for live timeout and
   namespace scope before running smoke.
7. Run targeted tests:
   - `pytest -q tests/test_diagnosis_llm_tooluse.py tests/test_incident_diagnosis.py`
   - `pytest -q tests/test_hermes_diagnosis_service.py tests/test_k8s_manifests.py`
   - any Loki/K8s guard tests touched by normalization.
8. Run `python -m compileall` for touched Python packages.
9. After user approval to implement and push, deploy the resulting image and run
   the live Alertmanager smoke against `dev-external/demo-apps`.

## Validation Targets

- No DeepSeek read timeout in Hermes logs during live smoke.
- Gateway logs show Alertmanager webhook 200 and diagnosis writeback 200.
- For the PodCrashLooping smoke, K8s read failures caused by invalid generated
  argv are reduced or eliminated.
- For the PodCrashLooping smoke, Loki failures caused by invalid generated args
  or cost overshoot are reduced.
- Remaining partial reasons are real data/topology gaps.

## Risk Areas

- Over-clamping may hide useful LLM intent.
- Too much service inference may select the wrong workload.
- Connector namespace scope must remain the source of truth; Hermes must not
  attempt to bypass it.

## Rollback Points

- Revert `toolsets/incident_diagnosis.py` normalization helpers if test fixtures
  show lower-quality evidence.
- Revert docs/tests only if they incorrectly encode current deployment contract.
