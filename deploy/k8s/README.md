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

## Image Tags And Digests

The split images are built from `Dockerfile.aiops` targets:

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

## Runtime Config

Runtime non-secret values are in `configmap.yaml` under `aiops-runtime-config`.

Important profile values:

- `AIOPS_CONNECTOR_URL`: Gateway to connector URL.
- `AIOPS_GATEWAY_URL`: Connector and Hermes to Gateway URL.
- `PROMETHEUS_URL`: Prometheus backend for `aiops-mcp-prometheus`.
- `LOKI_URL`: Loki backend for `aiops-mcp-loki`.
- `AIOPS_NAMESPACE_SCOPE`: connector namespace scope.

Copy `secret.example.yaml` to a private secret manifest if Feishu/model credentials are required:

```bash
cp deploy/k8s/secret.example.yaml /tmp/aiops-runtime-secret.yaml
kubectl apply -f /tmp/aiops-runtime-secret.yaml
```

The Deployments mark `aiops-runtime-secret` optional so health and profile smoke can run without real Feishu/model secrets.

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

To use a different namespace, change the `namespace:` field in the selected overlay.

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

Manual cleanup, when explicitly requested:

```bash
kubectl delete -k deploy/k8s/overlays/dev-bundled
```

## Alertmanager Target

The split Gateway currently exposes the Gateway service surface, not the legacy monolithic webhook server. For the old webhook path, keep using the legacy `aiops` image and `deploy/entrypoint.sh` until webhook routing is moved behind the split Gateway.
