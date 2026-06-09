# AIOps Native Kubernetes YAML

This directory provides native Kubernetes YAML for the split AIOps service images. It is intentionally not a Helm chart.

## Services

- `aiops-gateway`: K8s Gateway HTTP service on port `8080`.
- `aiops-connector`: cluster connector on port `8081` with a scoped ServiceAccount and Role.
- `aiops-hermes`: Hermes boundary on port `8082` with `/data` mounted from `aiops-hermes-data`.
- `aiops-mcp-prometheus`: Prometheus MCP HTTP service on port `8083`.
- `aiops-mcp-loki`: Loki MCP HTTP service on port `8084`.

Base manifests live in `deploy/k8s/*.yaml`. Kustomize overlays provide the dev profiles:

- `overlays/dev-bundled`: deploys AIOps plus API-compatible bundled dev Prometheus/Loki backends, `payment-api`, and a synthetic Loki log Job. The dev backends run from the same registry as the AIOps images so the development cluster does not depend on Docker Hub pulls.
- `overlays/dev-external`: deploys AIOps and points MCP services at existing Prometheus/Loki endpoints.
- `overlays/dev-disabled`: deploys AIOps with `PROMETHEUS_URL` and `LOKI_URL` empty; MCP query calls should degrade with `backend_unavailable`.
- `overlays/rc-bundled-digest`: release-candidate bundled profile pinned to immutable CI image digests. It renders `aiops-loki-synthetic-log-rc` instead of the default fixed-name Job so a retained `aiops-loki-synthetic-log` Job with older mutable tags does not block apply.
- `overlays/dev-remediation-rbac`: opt-in RBAC extension for `pods/exec`, `pods/attach`, and workload `patch/update`. Do not apply it for the default health/validate profiles.

## Image Tags And Digests

The split images are built from repository Dockerfile path `Dockerfile.aiops`.

Service build targets:

| Service image | Docker target | Runtime copy scope |
| --- | --- | --- |
| legacy all-in-one `aiops` | `aiops` | `aiops/`, `apps/`, `hermes/`, `hooks/`, `runtime/`, `skills/`, `toolsets/`, `deploy/entrypoint.sh`, `deploy/hermes-config.template.yaml` |
| `aiops-gateway` | `gateway` | `apps/aiops_k8s_gateway/`, `apps/service_http.py`, `aiops/`, `runtime/service_image_smoke.py`, `deploy/entrypoint-gateway.sh` |
| `aiops-connector` | `connectors` | `apps/cluster_connector/`, `apps/service_http.py`, `aiops/`, `runtime/service_image_smoke.py`, `deploy/entrypoint-connector.sh` |
| `aiops-hermes` | `hermes` | `hermes/`, `apps/service_http.py`, `aiops/`, `runtime/` Hermes gateway files, `toolsets/`, `deploy/entrypoint-hermes.sh`, and the `hermes-agent` submodule package |
| `aiops-mcp-prometheus` | `mcp-prometheus` | `apps/mcp_prometheus/`, `apps/observability_http.py`, `apps/service_http.py`, `aiops/`, Prometheus/query/audit `toolsets` files, `runtime/service_image_smoke.py`, `deploy/entrypoint-mcp-prometheus.sh` |
| `aiops-mcp-loki` | `mcp-loki` | `apps/mcp_loki/`, `apps/observability_http.py`, `apps/service_http.py`, `aiops/`, Loki/query/audit `toolsets` files, `runtime/service_image_smoke.py`, `runtime/image_smoke.py`, `deploy/entrypoint-mcp-loki.sh` |

`.dockerignore` excludes non-runtime build-context content such as `.git`, `.github`, `.agents`, caches, `tests/`, `docs/`, `deploy/k8s/`, root docs, logs, and compose files. The Dockerfile must not use `COPY . /app`; each target should copy only the runtime files it needs.

Build examples:

```bash
docker build -f Dockerfile.aiops --target gateway -t registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:gateway-dev .
docker build -f Dockerfile.aiops --target connectors -t registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:connectors-dev .
docker build -f Dockerfile.aiops --target hermes -t registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:hermes-dev .
docker build -f Dockerfile.aiops --target mcp-prometheus -t registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-prometheus-dev .
docker build -f Dockerfile.aiops --target mcp-loki -t registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-dev .
```

Local images are only a platform smoke precheck. QA and release verification must use candidate image digests produced by GitHub Actions.

GitHub Actions publishes all split services to `registry.cn-hangzhou.aliyuncs.com/timelessmao/hub` using service-prefixed tags:

```text
gateway-latest
connectors-latest
hermes-latest
mcp-prometheus-latest
mcp-loki-latest
```

Branch candidate tags use `candidate-` with the same service prefix. SHA tags use the same service prefix and the short Git SHA. For example:

```text
registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:gateway-candidate-<branch>
registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:gateway-<short-sha>
```

For digest pinning, replace each Deployment `image:` value with the published digest form:

```yaml
image: registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:<gateway-digest>
```

Use one digest per split service Deployment.

The RC digest overlay is the one-command immutable deployment entry for PR #35 head `751ad23453eb329d5412dcec9054993ae306dfdd`. These digests come from the successful `docker-image` push workflow run <https://github.com/Timeless0927/AIops/actions/runs/27185187112> for short SHA `751ad23`, so they include the Gateway/Connector registration recovery commit:

```bash
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

It pins:

```text
gateway          sha256:9ce8bfc5eb1aa3cacc29141414193731f709e98554b9d74fb80a5b37a779e98d
connectors       sha256:282c1f9b1f7b6219779d1352250d6ccbee9d32c9eeb019c365895ae54ea95218
hermes           sha256:7e5faf6a95e4c8f18a0e9499e9a56b7e0299ff3924bce2ac1d9cafedee17af29
mcp-prometheus   sha256:48a2eea41e7ac584fcb2cf8b6d017f3b48ab1bea7eba578f718f78c997b19149
mcp-loki         sha256:9ff447468425f3d94316ab255296a8768a297269ad460d7a806c48081e7372b7
```

## Runtime Config

Runtime non-secret values are in `configmap.yaml` under `aiops-runtime-config`.

Important profile values:

- `AIOPS_CONNECTOR_URL`: Gateway to connector URL.
- `AIOPS_GATEWAY_URL`: Connector and Hermes to Gateway URL.
- `PROMETHEUS_URL`: Prometheus backend for `aiops-mcp-prometheus`.
- `LOKI_URL`: Loki backend for `aiops-mcp-loki`.
- `AIOPS_NAMESPACE_SCOPE`: connector namespace scope.

`secret.example.yaml` is an example file only. It is not part of the default base or dev profile kustomizations because applying a placeholder Secret would overwrite real credentials with `replace-me` values.

Create or update the real Secret in the same namespace as the selected profile before running real Feishu/model flows. Default dev namespace:

```bash
kubectl -n aiops-dev create secret generic aiops-runtime-secret \
  --from-literal=FEISHU_APP_ID='<replace-me>' \
  --from-literal=FEISHU_APP_SECRET='<replace-me>' \
  --from-literal=FEISHU_VERIFICATION_TOKEN='' \
  --from-literal=FEISHU_ENCRYPT_KEY='' \
  --from-literal=AIOPS_MODEL_API_KEY='<replace-me>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

If you change the overlay `namespace:` value, use that same namespace in `kubectl -n <namespace> create secret ...`. The Deployments mark `aiops-runtime-secret` optional so health and profile smoke can run with placeholders, but production-like Feishu/model flows require the namespace-local real Secret.

Do not apply `secret.example.yaml` directly to a namespace that already has real credentials unless you intentionally want to overwrite `aiops-runtime-secret` with placeholder values. If a dev-only placeholder Secret is needed for a future smoke profile, keep it in a clearly named opt-in overlay and delete it before using real credentials.

Before RC or product-like validation, verify the retained dev Secret is not still using the placeholder values:

```bash
kubectl -n aiops-dev get secret aiops-runtime-secret \
  -o jsonpath='{.data.FEISHU_APP_ID}' | base64 -d
kubectl -n aiops-dev get secret aiops-runtime-secret \
  -o jsonpath='{.data.AIOPS_MODEL_API_KEY}' | base64 -d
```

If either command prints `replace-me`, update the real Secret in `aiops-dev` with the namespace-local `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -` command above before validating Feishu/model flows. Applying any default or RC kustomize profile will not create or overwrite this Secret.

## RBAC Boundary

The default `aiops-connector` Role is read-only and supports observation/validation only:

- core resources `pods`, `pods/log`, `events`, `services`, `configmaps`: `get`, `list`, `watch`
- apps resources `deployments`, `statefulsets`, `daemonsets`, `replicasets`: `get`, `list`, `watch`

Mutation-capable permissions are not part of the default bundled/external/disabled profiles. To inspect the opt-in remediation RBAC:

```bash
kubectl kustomize deploy/k8s/overlays/dev-remediation-rbac
```

Apply it only for a controlled remediation test with the required approval/audit guardrails:

```bash
kubectl apply -k deploy/k8s/overlays/dev-remediation-rbac
```

## Deploy

Default development namespace is `aiops-dev`.

Bundled profile:

```bash
kubectl apply -k deploy/k8s/overlays/dev-bundled
```

External observability profile:

```bash
kubectl apply -k deploy/k8s/overlays/dev-external
```

Before applying `dev-external`, update `PROMETHEUS_URL` and `LOKI_URL` in `overlays/dev-external/kustomization.yaml`.

Disabled observability profile:

```bash
kubectl apply -k deploy/k8s/overlays/dev-disabled
```

RC digest-pinned bundled profile:

```bash
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

To use a different namespace, change the `namespace:` field in the selected overlay.

## Profile Switching And Deletion

The dev profiles share namespace `aiops-dev`. `kubectl apply -k` updates or creates resources but does not delete resources that are no longer part of the newly selected profile.

To switch from bundled to external or disabled without retaining bundled-only resources, delete the old profile first:

```bash
kubectl delete -k deploy/k8s/overlays/dev-bundled
kubectl apply -k deploy/k8s/overlays/dev-external
```

To switch back to bundled:

```bash
kubectl delete -k deploy/k8s/overlays/dev-external
kubectl apply -k deploy/k8s/overlays/dev-bundled
```

If remediation RBAC was applied, remove it independently when the controlled test ends:

```bash
kubectl delete -k deploy/k8s/overlays/dev-remediation-rbac
```

For AIO-71 development validation, keep `dev-bundled` resources after smoke unless explicitly asked to clean them up.

When switching from `dev-bundled` to `rc-bundled-digest`, existing Deployments and Services are updated in place. The default fixed-name Job `aiops-loki-synthetic-log` is not mutated because Kubernetes Job pod templates are immutable; the RC overlay creates `aiops-loki-synthetic-log-rc` with the pinned Loki digest. If the old default Job is no longer needed, delete it explicitly:

```bash
kubectl -n aiops-dev delete job aiops-loki-synthetic-log
```

## Verify

Wait for the core split services:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-gateway --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-connector --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-hermes --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-mcp-prometheus --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-mcp-loki --timeout=180s
```

Check health/readiness. The smoke commands use the published AIOps Python image instead of Docker Hub `curl` images so they can run in the development cluster registry path:

```bash
kubectl -n aiops-dev run aiops-health-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest \
  --command -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://aiops-gateway:8080/healthz', timeout=5).read().decode()); print(urllib.request.urlopen('http://aiops-connector:8081/healthz', timeout=5).read().decode()); print(urllib.request.urlopen('http://aiops-hermes:8082/readyz', timeout=5).read().decode())"
```

Check Gateway/Connector registration:

```bash
kubectl -n aiops-dev run aiops-gateway-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest \
  --command -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://aiops-gateway:8080/connectors', timeout=5).read().decode())"
```

Bundled Prometheus evidence:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-dev-prometheus --timeout=180s
kubectl -n aiops-dev run aiops-prom-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest \
  --command -- python3 -c "import json, urllib.request; payload={'request_id':'prom-smoke','cluster_id':'dev-bundled','reason':'k8s bundled smoke','query':'up','max_series':5}; req=urllib.request.Request('http://aiops-mcp-prometheus:8083/query_metrics', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

Bundled Loki evidence:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-dev-loki --timeout=180s
kubectl -n aiops-dev wait --for=condition=complete job/aiops-loki-synthetic-log --timeout=120s
kubectl -n aiops-dev run aiops-loki-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest \
  --command -- python3 -c "import json, urllib.request; payload={'request_id':'loki-smoke','cluster_id':'dev-bundled','reason':'k8s bundled smoke','query':'{app=\"payment-api\"}','time_range':{'type':'relative','value':'15m'},'max_lines':20}; req=urllib.request.Request('http://aiops-mcp-loki:8084/query_logs', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

For RC digest-pinned validation, wait on the RC Job name and use the RC cluster id:

```bash
kubectl -n aiops-dev wait --for=condition=complete job/aiops-loki-synthetic-log-rc --timeout=120s
kubectl -n aiops-dev run aiops-loki-rc-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:9ff447468425f3d94316ab255296a8768a297269ad460d7a806c48081e7372b7 \
  --command -- python3 -c "import json, urllib.request; payload={'request_id':'loki-rc-smoke','cluster_id':'rc-bundled-digest','reason':'k8s rc digest smoke','query':'{app=\"payment-api\"}','time_range':{'type':'relative','value':'15m'},'max_lines':20}; req=urllib.request.Request('http://aiops-mcp-loki:8084/query_logs', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

Disabled profile controlled degradation:

```bash
kubectl -n aiops-dev run aiops-disabled-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest \
  --command -- python3 -c "import json, urllib.request; payload={'request_id':'disabled-prom','cluster_id':'dev-disabled','reason':'disabled smoke','query':'up'}; req=urllib.request.Request('http://aiops-mcp-prometheus:8083/query_metrics', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); body=urllib.request.urlopen(req, timeout=10).read().decode(); print(body); assert 'backend_unavailable' in body"
```

## Retained Resources

For development validation requested in AIO-71, do not clean up the namespace after smoke. Leave these resources for inspection:

- namespace `aiops-dev`
- core Deployments and Services for Gateway, Connector, Hermes, MCP Prometheus, MCP Loki
- PVC `aiops-hermes-data`
- bundled profile Deployments and Services for Prometheus, Loki, and `payment-api`
- Job `aiops-loki-synthetic-log`
- RC digest overlay Job `aiops-loki-synthetic-log-rc` when `overlays/rc-bundled-digest` has been applied

Manual cleanup, when explicitly requested:

```bash
kubectl delete -k deploy/k8s/overlays/dev-bundled
```

## Alertmanager Target

The split Gateway currently exposes the Gateway service surface, not the legacy monolithic webhook server. For the old webhook path, keep using the legacy `aiops` image and `deploy/entrypoint.sh` until webhook routing is moved behind the split Gateway.
