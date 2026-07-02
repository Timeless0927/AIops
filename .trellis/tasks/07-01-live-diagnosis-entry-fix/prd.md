# Fix live diagnosis entry path

## Goal

Make the operator-facing live validation path prove real diagnosis, not just
Gateway handoff plus keyword fallback. The smoke path is:

1. POST an Alertmanager-shaped alert to Gateway `/webhooks/alertmanager`
2. Gateway creates an incident and hands off to Hermes
3. Hermes runs LLM tool-use diagnosis, collects multi-source evidence, and writes
   the result back to Gateway
4. The operator can read a stable diagnosis result and see whether it was
   `llm-tooluse-v1`

## Confirmed Facts

- Current live cluster image tag is `5d5b27e`; repo HEAD has later fixes in
  `af45aff` that are not in the running image.
- User-triggered live smoke produced a queued Hermes session, then returned
  `collector_version=incident_diagnosis/keyword-v1`, `status=partial`.
- Hermes logs show: `diagnosis LLM tool-use failed, falling back to keyword plan:
  provider endpoint unreachable: The read operation timed out`.
- Direct DeepSeek calls from inside the Hermes pod succeed quickly, including
  tool-call shape, so the key/base URL are usable.
- Provider timeout currently reuses `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS`, default
  `3s`; live diagnosis prompt/tool-use can exceed that.
- K8s evidence fails with `HTTP Error 401: Unauthorized` because Hermes calls
  Gateway `/k8s/read` without a bearer token while Gateway requires
  `PERMISSION_K8S_READ`.
- Gateway writeback fails because `AIOPS_GATEWAY_WRITEBACK_SECRET` is absent from
  the runtime secret/config.
- Current Alertmanager smoke used a synthetic `PodCrashLooping` alert but the
  actual Loki evidence contains heartbeat logs. This can validate the plumbing;
  root-cause quality requires a real fault signal.

## Requirements

1. Hermes must not call Gateway `/k8s/read` anonymously. Use a controlled
   service-to-service credential or token mechanism that still goes through
   Gateway authorization/audit.
2. The service credential must be deployable via `aiops-runtime-secret` and must
   not weaken browser/user authorization paths.
3. Live LLM provider calls must have a configurable timeout large enough for
   real diagnosis. The dev-external profile should set an explicit safe value.
4. Gateway/Hermes writeback must be configured with a shared
   `AIOPS_GATEWAY_WRITEBACK_SECRET` so successful diagnosis becomes durable in
   Gateway.
5. Deployment docs/config must make the required live-diagnosis secrets/env keys
   visible.
6. Tests must cover:
   - Hermes includes service auth on Gateway K8s-read calls when configured
   - Gateway accepts the configured service credential only for `k8s_read`
   - Existing bearer-token user auth remains required for normal protected APIs
   - Provider timeout env behavior remains covered

## Acceptance Criteria

- Unit tests for the changed Gateway/Hermes auth path pass.
- Targeted diagnosis/provider/export/replay tests still pass.
- A live cluster rerun can reach:
  - `collector_version=incident_diagnosis/llm-tooluse-v1`
  - no `HTTP Error 401` for `run_k8s_read`
  - `writeback.status=succeeded`
- If the live image is not yet rebuilt from the fixing commit, the final report
  must say that deployment is still pending and list the exact rollout command or
  image tag requirement.

## Out of Scope

- Building a full Console diagnosis UI.
- Implementing mutation/remediation execution.
- Improving root-cause quality beyond making the live diagnosis path actually
  exercise LLM tool-use and evidence collection.
- Replacing Gateway RBAC with a broad internal bypass.
