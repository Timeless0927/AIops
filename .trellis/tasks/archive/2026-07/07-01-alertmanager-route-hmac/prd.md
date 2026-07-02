# Alertmanager automatic route and bearer auth

## Goal

Route real Alertmanager notifications into Gateway `/webhooks/alertmanager` so
incidents and Hermes diagnosis start automatically, with a Gateway-validated
Alertmanager bearer token configured and tested instead of relying only on
manual POST smoke commands.

## Confirmed Facts

- Gateway already exposes `/webhooks/alertmanager` and supports optional
  `ALERTMANAGER_WEBHOOK_SECRET` / `AIOPS_ALERTMANAGER_WEBHOOK_SECRET` HMAC
  validation.
- Previous dev-external validation intentionally used manual POST to avoid
  coupling evidence-chain validation to Alertmanager routing.
- Real automatic routing was explicitly deferred as an independent task.
- `kube-prometheus-stack` / Prometheus Operator `AlertmanagerConfig`
  `webhookConfigs` can configure a webhook URL and HTTP client auth, including a
  bearer token Secret reference, but it cannot compute a per-request
  `X-Signature` HMAC over the JSON body for a generic webhook receiver.
- For automatic Alertmanager routing, the selected contract is a static bearer
  credential on Gateway `/webhooks/alertmanager`, scoped only to that route.
  HMAC remains useful for callers that can sign a body, but it is not the
  automatic Alertmanager route contract.
- The existing cluster-internal smoke command and
  `deploy/k8s/smoke/trigger-agent-job.yaml` can sign requests because they run
  Python before posting to Gateway; that is not the same as automatic
  Alertmanager routing.

## Requirements

1. Configure a real Alertmanager receiver/route for the dev-external validation
   environment without breaking existing manual smoke paths.
2. Configure a shared bearer token for Alertmanager and Gateway, keeping real
   secret values out of git.
3. Add or document a low-noise test alert path that can trigger one automatic
   diagnosis flow on demand.
4. Gateway must reject missing or invalid bearer tokens when
   `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is configured.
5. Deployment docs must describe how to enable, verify, and disable the route.

## Acceptance Criteria

- A real Alertmanager notification creates or reuses an incident through Gateway.
- Hermes diagnosis is triggered from the automatic route.
- Bearer-authenticated webhook requests pass and missing/bad bearer tokens fail
  with a controlled error when the token env is configured.
- Manual cluster-internal smoke remains available for diagnosis debugging.

## Decision

- Use Alertmanager-native bearer auth instead of a signing relay. A relay would
  only be meaningful if NetworkPolicy or mTLS restricted relay callers to
  Alertmanager; without that, any pod could call the relay and receive a signed
  Gateway request. Bearer auth is simpler to operate and makes the trust boundary
  explicit: anyone with the token can send Alertmanager webhook payloads, so the
  token must be per-environment and protected by Kubernetes Secret and network
  policy where available.

## Out of Scope

- Improving diagnosis quality.
- Building Console UI for routed alerts.
- Adding mutation execution.
- Building a signing relay for Alertmanager HMAC.
