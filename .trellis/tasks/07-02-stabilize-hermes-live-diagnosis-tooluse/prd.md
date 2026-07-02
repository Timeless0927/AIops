# Stabilize Hermes live diagnosis tool-use quality

## Goal

Reduce live Hermes diagnosis sessions that end `partial` for avoidable tool-use
reasons after Alertmanager handoff is working. A live Alertmanager-triggered
PodCrashLooping diagnosis should keep the DeepSeek tool-use path alive, collect
K8s/log/metric evidence from the configured diagnosis namespace, and write back
diagnosis artifacts with only real data gaps marked missing.

## User Value

The operator can trust the automatic alert-to-diagnosis path enough to use it
for iterative live validation: transport failures, config drift, invalid tool
arguments, and obvious target-field inference mistakes should not dominate the
diagnosis result.

## Confirmed Facts

- Live `aiops-dev` was running Gateway/Hermes image tag `155991f`.
- DeepSeek base connectivity and simple tool calling worked from inside Hermes.
- The observed DeepSeek tool-use timeout came from Hermes falling back to the
  3s default when live `aiops-runtime-config` lacked
  `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS`.
- Patching live `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS=30` and restarting Hermes
  removed `tool-use failed`, `provider endpoint unreachable`, and
  `read operation timed out` from the verification window.
- `deploy/k8s/overlays/dev-external/kustomization.yaml` already declares
  `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS=30`, but live drift still occurred.
- The final live smoke used `cluster=dev-external` and `namespace=demo-apps`;
  session `diagnosis-3405a5a6a10149a99565f49634e87791` wrote back
  successfully but remained `partial`: 72 steps, 64 succeeded, 8 missing.
- Remaining avoidable misses were: 3 failed `run_k8s_read` calls from HTTP 400,
  4 failed `query_logs` calls from invalid args or cost limits, and 1 topology
  miss for `dev-external/demo-apps/demo-apps`.
- Gateway/connector read path itself is healthy for allowed scope:
  `dev-external/demo-apps` manual `/k8s/read` returned HTTP 200 and
  `status=succeeded`.
- Connector namespace scope is separate from Gateway service actor scope; live
  connector scope was `aiops-dev,demo-apps`, so `default` was correctly rejected.
- Alertmanager extraction supports service labels (`service`, `service_name`,
  `app.kubernetes.io/name`, `app`) and workload/pod fields, but diagnosis falls
  back to `service = app = namespace` when no service/app is present.
- LLM-provided tool arguments currently override default args directly; this can
  preserve invalid `argv`, unbounded Loki windows, or weak topology service
  names unless normalized after merge.
- Current `partial` evidence points to boundary/tool-use quality issues, not a
  lack of a multi-agent runtime: provider connectivity works, writeback works,
  and the remaining avoidable misses are invalid K8s/Loki/topology arguments.
- ADR-0003 already accepted the thin LLM tool-use brain and explicitly rejected
  a self-improving external agent runtime because diagnosis needs auditable,
  reproducible evidence collection, not autonomous skill mutation.

## Requirements

0. Scope this slice to the live PodCrashLooping path in `dev-external/demo-apps`;
   broad diagnosis-quality improvements across other alert classes are deferred.
1. Make the 30s Hermes provider/tool timeout an auditable deployment contract for
   live tool-use profiles, with tests that fail if `dev-external` stops rendering
   it.
2. Keep manual live patches and rendered manifests aligned; document the exact
   drift check operators should run before smoke validation.
3. Normalize or reject LLM-supplied `run_k8s_read` args so generated requests
   remain read-only, namespace-scoped, connector-scope-compatible, and prefer
   the Alertmanager pod/workload target when available.
4. Normalize or clamp LLM-supplied `query_logs` args so generated requests stay
   within supported `time_range`, `max_lines`, and scoped LogQL limits while
   preserving useful evidence.
5. Improve topology service selection so namespace fallbacks do not create
   misleading lookups such as `dev-external/demo-apps/demo-apps` when better
   pod/workload/service labels exist.
6. Preserve strict safety behavior: invalid or unsafe tool args must fail closed
   or be clamped to safe read-only defaults, never broaden namespace or mutation
   scope.
7. Keep the existing Alertmanager -> Gateway -> Hermes -> Gateway writeback
   contract compatible.
8. Record remaining genuine data gaps as `partial` with clear reasons.

## Acceptance Criteria

- The code changes are limited to the PodCrashLooping/tool-use stabilization
  needed for `dev-external/demo-apps`; unrelated alert classes keep existing
  behavior unless they share the same safe normalizer path.
- `kustomize build deploy/k8s/overlays/dev-external` renders
  `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS: "30"` in `aiops-runtime-config`.
- A test asserts `dev-external` renders the timeout and that deployment docs list
  it in the live smoke/drift checklist.
- Unit tests cover LLM tool arg normalization for:
  - `run_k8s_read` rejecting or replacing unsafe/invalid argv while preserving
    namespace and read-only scope.
  - `query_logs` clamping excessive windows/line counts and retaining required
    `cluster_id`, `time_range`, and `reason`.
  - `get_service_topology` using a better service identity when Alertmanager
    labels or workload fields provide one, and not blindly using namespace as
    service when that would be low-confidence.
- Existing targeted tests for Alertmanager handoff, Hermes diagnosis service,
  Loki facade, K8s read guard, and manifest rendering pass.
- A live `aiops-dev` smoke using `cluster=dev-external`,
  `namespace=demo-apps`, and a real service/app/workload signal completes with
  `writeback.status=succeeded`, no DeepSeek provider timeout log, and fewer
  avoidable missing evidence entries than the baseline session.
- If live still returns `partial`, the missing evidence reasons are attributable
  to real backend/topology/log data gaps, not invalid generated tool arguments or
  missing timeout config.

## Out of Scope

- Autonomous remediation or mutation execution.
- Feishu approval workflow changes.
- Reworking the complete LLM provider architecture.
- Replacing the thin Hermes diagnosis brain with LangGraph, CrewAI, AutoGen, or
  another full agent framework for this stabilization slice.
- Fixing the separate GitHub `smoke-service-compose` failure unless it blocks
  this task's targeted tests.
- Expanding the real fault replay fixture corpus.

## Open Questions

- Should we revisit a framework migration later only if thin orchestration grows
  durable resume, multi-agent delegation, or complex human-in-the-loop state
  needs that the current loop cannot cover cheaply?

## Decisions

- 2026-07-02: Target the narrow MVP: PodCrashLooping in `dev-external/demo-apps`
  only. The goal is to stabilize the real alert-to-diagnosis chain before
  broadening diagnosis quality across alert classes.
