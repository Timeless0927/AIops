# Design: Stabilize Hermes live diagnosis tool-use quality

## Boundaries

- Gateway remains the Alertmanager ingress and diagnosis writeback owner.
- Hermes remains the diagnosis orchestrator and tool-use loop owner.
- Connector remains the enforcement point for namespace scope and kubectl
  execution.
- MCP facades remain the enforcement points for Prometheus, Loki, and topology
  query contracts.

## Approach

### Narrow Slice

This stabilization slice targets PodCrashLooping in `dev-external/demo-apps`.
Shared helpers may improve sibling paths incidentally, but the implementation
should not broaden into a general alert-quality rewrite.

### Deployment Contract

`dev-external` is the live tool-use profile. It must render the same
`AIOPS_HERMES_TOOL_TIMEOUT_SECONDS=30` value that was patched into live during
the timeout investigation. The repository already contains the patch, so the
implementation should add or tighten manifest tests and docs instead of adding a
second configuration source.

### LLM Tool Argument Normalization

`toolsets.incident_diagnosis._build_tool_args_from_llm` currently builds safe
defaults and then lets non-empty LLM fields override those defaults. Keep that
extension point, but route merged args through per-tool normalizers:

- `run_k8s_read`: accept only argv arrays that pass read-only shape checks and
  preserve the incident namespace. For invalid argv, fall back to the default
  safe `kubectl get pods -n <namespace> [-l <selector>]` or a targeted
  pod/workload read when Alertmanager provided a PodCrashLooping target.
- `query_logs`: preserve caller intent when valid, but clamp unsupported
  `time_range`, excessive windows, and excessive `max_lines` to bounded defaults.
  Keep scoped default LogQL for the incident namespace/service/workload when LLM
  emits empty or overly broad queries.
- `get_service_topology`: derive service identity from explicit service/app
  labels first, then workload fields, then pod-derived workload hints. Namespace
  as service should be treated as low-confidence and avoided when it would create
  `<cluster>/<namespace>/<namespace>` lookups.

### Alert Target Enrichment

Gateway already extracts `service`, pod, container, and workload fields into the
alert payload. Hermes should consume those fields when constructing incident
tool args. This avoids changing the Gateway/Hermes handoff contract while still
using the data already present in the payload.

### Failure Semantics

Unsafe or impossible tool calls must not broaden access. The normalizers should
either clamp to known safe defaults or allow the downstream tool to fail closed
with a clear `missing_reason`. The target state is fewer avoidable failures, not
masking real data gaps.

## Compatibility

- No API route changes are required.
- Existing queued session behavior remains compatible, though the planning notes
  call out that queued is not an intermediate progress signal.
- Existing tests that assert partial behavior for unavailable backends should
  remain valid.

## Rollout

1. Land code/tests/docs.
2. Let GitHub CI build images.
3. Roll Hermes and affected MCP/Gateway images only if touched.
4. Confirm live config matches rendered `dev-external`.
5. Rerun Alertmanager smoke in `dev-external/demo-apps`.

## Rollback

- Revert code changes and redeploy previous image tag if normalization regresses
  diagnosis quality.
- Keep the live timeout env at `30`; lowering it would reintroduce the observed
  provider timeout.
