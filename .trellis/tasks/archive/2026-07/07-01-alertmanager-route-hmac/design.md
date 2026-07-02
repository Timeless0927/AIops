# Design - Alertmanager automatic route and bearer auth

## Boundary

The task crosses three runtime boundaries:

- kube-prometheus-stack Alertmanager -> AIOps alert ingress
- AIOps alert ingress -> Gateway `/webhooks/alertmanager`
- Gateway -> Hermes diagnosis handoff

Gateway remains the only service that creates incidents and starts Hermes
diagnosis sessions. The new work should make Alertmanager delivery automatic
using Alertmanager-native HTTP authentication.

## Key Constraint And Decision

Prometheus Operator `AlertmanagerConfig.webhookConfigs` supports a generic
webhook URL and HTTP client auth fields, including bearer token Secret
references, but it does not support computing a body-bound HMAC signature such
as `X-Signature: sha256=<hmac(body)>` per request.

Selected approach: direct Alertmanager -> Gateway using bearer auth. Gateway
will validate `Authorization: Bearer <token>` against
`AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` only for `/webhooks/alertmanager`.

Not selected: an Alertmanager signing relay. A relay only preserves the HMAC
security property if NetworkPolicy/mTLS ensures only Alertmanager can call the
relay and only the relay can call Gateway. Without that network boundary, any
pod could send an unsigned body to the relay and have it signed.

## Contract

Gateway env:

- `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN`: optional static bearer token. If set,
  `/webhooks/alertmanager` requires `Authorization: Bearer <token>`.

Gateway behavior:

- If `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is unset, preserve current unsigned dev
  behavior and optional HMAC behavior for manual callers.
- If bearer token is configured, missing or mismatched bearer token returns
  `401` with a controlled webhook error body.
- If both bearer token and HMAC secret are configured, bearer auth is required
  for the Alertmanager route. HMAC should not be required for Alertmanager
  automatic route because Alertmanager cannot produce it.
- The bearer token is scoped only to `/webhooks/alertmanager`; it must not affect
  `/api/*`, `/k8s/read`, `/diagnosis/writeback`, incident view, approval, or
  notification routes.

Alertmanager behavior:

- `AlertmanagerConfig.webhookConfigs[].httpConfig.authorization.credentials`
  references a Secret key containing the bearer token.
- The webhook URL points directly to
  `http://aiops-gateway.aiops-dev.svc.cluster.local:8080/webhooks/alertmanager`
  or the namespace-appropriate Gateway Service.

## Deployment Shape

Repo-owned Kubernetes assets should live outside the main dev-external overlay
unless the resources are in `aiops-dev`. The monitoring stack normally lives in
`loki` or `monitoring`, and Kustomize namespace transformation from
`overlays/dev-external` would otherwise move those resources into `aiops-dev`.

Proposed files:

- `deploy/k8s/alertmanager/aiops-alertmanager-route.yaml`: an
  `AlertmanagerConfig` in the monitoring namespace that routes a low-noise test
  matcher and desired production matchers directly to Gateway with bearer auth.
- `deploy/k8s/alertmanager/secret.example.yaml`: placeholder bearer token Secret
  for the monitoring namespace only.
- Gateway runtime Secret examples include `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` so
  Gateway and Alertmanager can share the token without committing real values.
- `deploy/k8s/README.md`: commands to create real secrets, apply/disable the
  route, trigger a one-shot test alert, and keep manual smoke available.

## Compatibility

- Existing manual `kubectl run ... POST /webhooks/alertmanager` remains
  available. When Gateway bearer auth is enabled, the documented smoke must send
  `Authorization: Bearer <token>`.
- Existing Gateway HMAC tests remain valid and should be extended only if the
  HMAC path changes. This task should add bearer-specific tests rather than
  deleting HMAC coverage.
- Hermes writeback HMAC remains unchanged.

## Rollback

- Delete the `AlertmanagerConfig` route or change its receiver back to `null`.
- Remove `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` from Gateway only if unsigned manual
  debugging is intentionally desired.
- Delete the monitoring namespace bearer-token Secret if automatic Alertmanager
  delivery is disabled.
