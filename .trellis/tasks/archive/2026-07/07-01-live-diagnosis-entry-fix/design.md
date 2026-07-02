# Design — live diagnosis entry fix

## Boundary

The failure crosses three runtime boundaries:

- Hermes -> provider: OpenAI-compatible `/chat/completions`
- Hermes -> Gateway: read-only `/k8s/read`
- Hermes -> Gateway: signed `/diagnosis/writeback`

We keep Gateway as the owner of K8s authorization. Hermes gets a narrow service
credential for read-only diagnosis calls instead of bypassing Gateway.

## Service Credential Contract

Add two optional env keys:

- `AIOPS_GATEWAY_SERVICE_TOKEN`: configured on Gateway and Hermes with the same
  opaque secret value.
- `AIOPS_HERMES_GATEWAY_SERVICE_TOKEN`: optional Hermes-side override; if absent,
  Hermes reuses `AIOPS_GATEWAY_SERVICE_TOKEN`.

Gateway behavior:

- For `/k8s/read`, before normal bearer session lookup, accept
  `Authorization: Bearer <AIOPS_GATEWAY_SERVICE_TOKEN>` as actor
  `aiops-hermes` with role `oncall_approver`, scope namespace `*`.
- This service token is only honored for `PERMISSION_K8S_READ`. Other protected
  routes still require normal user sessions.
- Audit records keep actor/username as `aiops-hermes` and decision `allow` or
  `deny` as usual.

Hermes behavior:

- `_http_tool_adapter` accepts optional headers.
- `_k8s_read_adapter` injects `Authorization: Bearer <token>` only when a token
  env exists.
- If no token is configured, behavior remains unchanged and Gateway returns a
  controlled 401.

## Timeout Contract

Provider timeout already reads `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS`. The
dev-external config should set it explicitly to `30` so live LLM tool-use does
not fall back due to the 3s default.

## Writeback Contract

`AIOPS_GATEWAY_WRITEBACK_SECRET` must exist in `aiops-runtime-secret` and be
visible to both Gateway and Hermes via existing `envFrom`.

## Deployment / Runtime Notes

- Running cluster currently uses image tag `5d5b27e`; the repo has later commits.
  The fix must be built and deployed before live validation can fully pass.
- Kustomize config should document the new timeout/service-token/writeback keys,
  but real secret values stay outside git.

## Tradeoffs

- Chosen: narrow shared service token for read-only K8s evidence.
- Not chosen: unauthenticated internal `/k8s/read`; too broad and breaks Gateway
  audit/authorization assumptions.
- Not chosen: Hermes logs in as a static user on every session; adds token
  lifecycle and identity config coupling for a service-to-service hop.
