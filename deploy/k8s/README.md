# AIOps Native Kubernetes YAML

This directory provides native Kubernetes YAML for the split AIOps service images. It is intentionally not a Helm chart.

## Services

- `aiops-gateway`: K8s Gateway HTTP service on port `8080`; Console source lives in `apps/aiops_console_web`, builds as an independent artifact, and talks to Gateway `/api/*` and `/auth/*`.
- `aiops-connector`: cluster connector on port `8081` with a scoped ServiceAccount and Role.
- `aiops-diagnosis`: diagnosis boundary on port `8082` with `/data` mounted from `aiops-diagnosis-data`.
- `aiops-notification`: internal Notification Engine on port `8086` with `notification.db` on `aiops-notification-data`; only Gateway may call its acceptance API.
- `aiops-mcp-prometheus`: Prometheus MCP HTTP service on port `8083`.
- `aiops-mcp-loki`: Loki MCP HTTP service on port `8084`.
- `aiops-mcp-topology`: Topology MCP HTTP service on port `8085`.

Base manifests live in `deploy/k8s/base/`; `deploy/k8s/kustomization.yaml` composes the base with `namespace.yaml`. Kustomize overlays provide the dev profiles:

- `overlays/dev-bundled`: deploys AIOps plus API-compatible bundled dev Prometheus/Loki backends, `payment-api`, and a synthetic Loki log Job. The dev backends run from the same registry as the AIOps images so the development cluster does not depend on Docker Hub pulls.
The `dev-external` profile points MCP services at existing Prometheus/Loki backends and opens the connector to the namespaces you actually want to diagnose. Before applying `dev-external`, edit `AIOPS_NAMESPACE_SCOPE` in `overlays/dev-external/kustomization.yaml` to the comma-separated list of business namespaces to diagnose — it defaults to `default,prod` as a placeholder, not `aiops-dev` (the AIOps platform namespace is usually not a diagnosis target, and pinning the scope there makes K8s evidence collection a no-op). Also confirm `PROMETHEUS_URL` and `LOKI_URL` point at backends that carry real data for those namespaces; the in-file comments mark the lines to edit.
- `overlays/dev-disabled`: deploys AIOps with `PROMETHEUS_URL` and `LOKI_URL` empty; MCP query calls should degrade with `backend_unavailable`.
- `overlays/rc-bundled-digest`: release-candidate bundled profile pinned to immutable CI image digests. It renders head-scoped Job `aiops-loki-synthetic-log-rc-454bd0c` instead of reusing the default or previous RC fixed-name Jobs, so retained Jobs with older immutable pod templates do not block apply. It includes Topology MCP with a pinned split-image digest and points diagnosis service at `http://aiops-mcp-topology:8085`.
- `overlays/dev-remediation-rbac`: opt-in RBAC extension for `pods/exec`, `pods/attach`, and workload `patch/update`. Do not apply it for the default health/validate profiles.

## Image Tags And Digests

The split images are built from repository Dockerfile path `Dockerfile.aiops`.

Service build targets:

| Service image | Docker target | Runtime copy scope |
| --- | --- | --- |
| `aiops-gateway` | `gateway` | `apps/aiops_k8s_gateway/`, `apps/service_http.py`, `aiops/`, `runtime/service_image_smoke.py`, `deploy/entrypoint-gateway.sh` |
| `aiops-connectors` | `connectors` | `apps/cluster_connector/`, `apps/service_http.py`, `aiops/`, `runtime/service_image_smoke.py`, `deploy/entrypoint-connector.sh` |
| `aiops-diagnosis` | `diagnosis` | `diagnosis_service/`, `apps/service_http.py`, `aiops/`, `toolsets/`, `runtime/` smoke/worker helpers, and `deploy/entrypoint-diagnosis.sh` |
| `aiops-notification` | `notification` | `notification_service/`, `apps/internal_auth.py`, `apps/service_http.py`, `aiops/contracts/`, `deploy/entrypoint-notification.sh` |
| `aiops-mcp-prometheus` | `mcp-prometheus` | `apps/mcp_prometheus/`, `apps/observability_http.py`, `apps/service_http.py`, `aiops/`, Prometheus/query/audit `toolsets` files, `runtime/service_image_smoke.py`, `deploy/entrypoint-mcp-prometheus.sh` |
| `aiops-mcp-loki` | `mcp-loki` | `apps/mcp_loki/`, `apps/observability_http.py`, `apps/service_http.py`, `aiops/`, Loki/query/audit `toolsets` files, `runtime/service_image_smoke.py`, `deploy/entrypoint-mcp-loki.sh` |
| `aiops-mcp-topology` | `mcp-topology` | `apps/mcp_topology/`, `apps/observability_http.py`, `apps/service_http.py`, `aiops/`, topology store `toolsets` files, `runtime/service_image_smoke.py`, `deploy/entrypoint-mcp-topology.sh` |

`.dockerignore` excludes non-runtime build-context content such as `.git`, `.github`, `.agents`, caches, `tests/`, `docs/`, `deploy/k8s/`, root docs, logs, and compose files. The Dockerfile must not use `COPY . /app`; each target should copy only the runtime files it needs.

Build examples:

```bash
docker build -f Dockerfile.aiops --target gateway -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:dev .
docker build -f Dockerfile.aiops --target connectors -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors:dev .
docker build -f Dockerfile.aiops --target diagnosis -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-diagnosis:dev .
docker build -f Dockerfile.aiops --target notification -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-notification:dev .
docker build -f Dockerfile.aiops --target mcp-prometheus -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus:dev .
docker build -f Dockerfile.aiops --target mcp-loki -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:dev .
docker build -f Dockerfile.aiops --target mcp-topology -t registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-topology:dev .
```

Local images are only a platform smoke precheck. QA and release verification must use candidate image digests produced by GitHub Actions.

GitHub Actions publishes each production split service to its own repository so rendered Kubernetes YAML remains auditable from `kubectl get deployments -o yaml`:

```text
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-diagnosis
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-notification
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-topology
```

Each repository uses unprefixed `latest`, `candidate-<branch>`, and `<short-sha>` tags because the repository name already identifies the service. For example:

```text
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:candidate-<branch>
registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:<short-sha>
```

For digest pinning, replace each Deployment `image:` value with the published digest form:

```yaml
image: registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@sha256:<gateway-digest>
```

Use one digest per split service Deployment.

Bundled dev/test observability components intentionally reuse the corresponding MCP service images: `aiops-dev-prometheus` and the synthetic `payment-api` use `aiops-mcp-prometheus`, while `aiops-dev-loki`, Loki smoke helpers, and synthetic Loki Jobs use `aiops-mcp-loki`. They run inline Python compatibility handlers from the manifest, not separate production Prometheus or Loki server binaries.

The RC digest overlay is the one-command immutable deployment entry for PR #45 head `454bd0cdb16e07b2f585a479af6618caf2dbd744`. These digests come from the successful `docker-image` push workflow run <https://github.com/Timeless0927/AIops/actions/runs/27590646477> for short SHA `454bd0c`, so they include the Topology MCP split image runtime and the platform packaging handoff:

```bash
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

It pins:

```text
gateway          sha256:e556a2d841259f410581abca35ab4b46d1af7520c85f392df07c32b8e00f0f14
connectors       sha256:e47339f603a32a496e5b1b203f205ce916192deb73f5eeb4c3b0649536b8a5eb
diagnosis        sha256:6da975fb5962872b6659b6b83d96c327bc29b7ddd57d52aac38d06f772803083
mcp-prometheus   sha256:3d56acc88c1ae40ecec8ccf4374501e8e171d2c5d367849f280222266ae87ce8
mcp-loki         sha256:0df45dfed0c7a674f3c5a0c26180c84c707bd82013b545b05654adf0a0df5172
mcp-topology     sha256:601f68d70efb7ace60a14b179129473a43e14c80acc54c5ca0b9d5564b75b68d
```

## Runtime Config

Runtime non-secret values are in `base/configmap.yaml` under `aiops-runtime-config`.

Important profile values:

- `AIOPS_CONNECTOR_URL`: Gateway to connector URL.
- `AIOPS_GATEWAY_URL`: Diagnosis service 的内部 Gateway URL。
- `AIOPS_CONNECTOR_GATEWAY_URL`: Connector 使用的外部 HTTPS Gateway URL；只有 Docker Compose 等显式 development smoke 才设置 `AIOPS_CONNECTOR_ALLOW_INSECURE_GATEWAY=true`。
- `AIOPS_CONNECTOR_CREDENTIAL`: `/admin` 创建 Connector Enrollment 时一次性返回的独立 credential；只保存在受管 Cluster 的 Secret 中。
- `AIOPS_CONNECTOR_HEARTBEAT_SECONDS`: Connector 主动注册和 heartbeat 周期，默认 `30` 秒。
- `AIOPS_DIAGNOSIS_URL`: Gateway to diagnosis service handoff URL for Alertmanager diagnosis sessions.
- `AIOPS_DIAGNOSIS_PATH`: diagnosis session trigger path, default `/diagnosis/sessions`.
- `AIOPS_CONSOLE_BASE_URL`: internal Console base URL used by Feishu notification-only buttons.
- `AIOPS_NOTIFICATION_CHANNELS_JSON`: service/team to Feishu chat mapping for Gateway Notification Center.
- `AIOPS_NOTIFICATION_MAX_ATTEMPTS`: max Feishu delivery attempts before dead-letter.
- `AIOPS_NOTIFICATION_RETRY_DELAY_SECONDS`: retry delay for failed notification deliveries.
- `PROMETHEUS_URL`: Prometheus backend for `aiops-mcp-prometheus`.
- `LOKI_URL`: Loki backend for `aiops-mcp-loki`.
- `AIOPS_TOPOLOGY_MCP_URL`: diagnosis topology MCP URL for `get_service_topology`.
- `AIOPS_NAMESPACE_SCOPE`: connector namespace scope — comma-separated list of namespaces to collect Kubernetes evidence from. Must cover the real diagnosis targets; `aiops-dev` (the AIOps platform namespace) is usually not a diagnosis target.
- `AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION`: connector mutation execution gate. Default is `false`; leave it false for all read-only diagnosis profiles.
- `AIOPS_DIAGNOSIS_TOOL_TIMEOUT_SECONDS`: shared diagnosis tool/provider timeout. Set this explicitly for live LLM tool-use profiles; `dev-external` uses `30`.
- `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN`: bearer token accepted only by Gateway `/webhooks/alertmanager` for Alertmanager automatic routing.
- `AIOPS_INTERNAL_TOKEN_FILE`: Kubernetes 注入的 `aiops-internal` audience projected token 路径；进程会在每次内部请求前重新读取，以支持短期 token 轮换。
- `AIOPS_POD_NAMESPACE`: 由 Downward API 注入，用于将 TokenReview 返回的 ServiceAccount identity 限制在当前 namespace。

Console 首次登录使用 `aiops-runtime-secret` 中的 `AIOPS_BOOTSTRAP_ADMIN_PASSWORD`，用户名默认为 `admin`。Gateway 只把 Argon2id hash 写入 `gateway.db`；不要在 ConfigMap 中保存明文密码。

`base/secret.example.yaml` is an example file only. It is not part of the default base or dev profile kustomizations because applying a placeholder Secret would overwrite real credentials with `replace-me` values.

Create or update the real Secret in the same namespace as the selected profile before running real Feishu/model flows. Default dev namespace:

```bash
kubectl -n aiops-dev create secret generic aiops-runtime-secret \
  --from-literal=AIOPS_BOOTSTRAP_ADMIN_PASSWORD='<strong-bootstrap-password>' \
  --from-literal=FEISHU_APP_ID='<real-feishu-app-id>' \
  --from-literal=FEISHU_APP_SECRET='<real-feishu-app-secret>' \
  --from-literal=FEISHU_VERIFICATION_TOKEN='' \
  --from-literal=FEISHU_ENCRYPT_KEY='' \
  --from-literal=AIOPS_MODEL_API_KEY='<real-model-api-key>' \
  --from-literal=AIOPS_ALERTMANAGER_WEBHOOK_TOKEN='<opaque-alertmanager-webhook-token>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

If you change the overlay `namespace:` value, use that same namespace in `kubectl -n <namespace> create secret ...`. The Deployments mark `aiops-runtime-secret` optional so health and profile smoke can run with placeholders, but production-like Feishu/model flows require the namespace-local real Secret.

Do not apply `base/secret.example.yaml` directly to a namespace that already has real credentials unless you intentionally want to overwrite `aiops-runtime-secret` with placeholder values. If a dev-only placeholder Secret is needed for a future smoke profile, keep it in a clearly named opt-in overlay and delete it before using real credentials.

Before RC or product-like validation, verify the retained dev Secret is not still using the placeholder values:

```bash
kubectl -n aiops-dev get secret aiops-runtime-secret \
  -o jsonpath='{.data.FEISHU_APP_ID}' | base64 -d
kubectl -n aiops-dev get secret aiops-runtime-secret \
  -o jsonpath='{.data.AIOPS_MODEL_API_KEY}' | base64 -d
```

If either command prints `replace-me`, update the real Secret in `aiops-dev` with the namespace-local `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -` command above before validating Feishu/model flows. A retained placeholder Secret is not a valid real configuration and a reality/product-like validation must not pass Feishu/model checks while it remains in that state. Applying any default or RC kustomize profile will not create or overwrite this Secret.

## RBAC Boundary

Gateway、Diagnosis 和三个 MCP 进程分别使用独立 ServiceAccount。每个 Pod 挂载 audience 为 `aiops-internal`、有效期一小时的 projected token；接收端通过 Kubernetes TokenReview 认证，并按调用链中的 ServiceAccount 名称授权。`aiops-token-reviewer` ClusterRole 只授予 `tokenreviews.create`，不分发内部静态 secret。

`aiops-diagnosis` 只接受 Gateway Pod ingress，三个 MCP Service 只接受 Diagnosis Pod ingress。它们的 Service 均保持 ClusterIP，未配置 NodePort 或公共 Ingress。Gateway 同时承载 Console、Alertmanager 与 Connector 的外部身份边界，因此其 NetworkPolicy 不能按 HTTP path 隔离；`/diagnosis/writeback`、内部 Incident view 与 Diagnosis `/k8s/read` 调用在应用层强制 TokenReview。Console session、Alertmanager token 和 Connector Enrollment 不使用该 Service Identity。

Gateway 的 TLS Ingress 只转发 `/auth`、`/api`、`/webhooks/alertmanager` 和 `/connectors`；它不转发 `/diagnosis`、`/incidents` 或 `/k8s/read`。部署前将 `aiops.example.com` 和 `aiops-gateway-tls` 替换为真实 host 与 TLS Secret。缺少 TLS Secret 或 Ingress controller 时，外部入口应保持不可用，不得改回 NodePort 绕过。

Docker Compose 只用于镜像、健康检查和本地连通性 smoke；受保护的内部业务 route 只在 Kubernetes 部署中受支持。

The default `aiops-connector` Role is read-only and supports observation/validation only:

- core resources `pods`, `pods/log`, `events`, `services`, `configmaps`: `get`, `list`, `watch`
- apps resources `deployments`, `statefulsets`, `daemonsets`, `replicasets`: `get`, `list`, `watch`

Mutation-capable permissions are not part of the default bundled/external/disabled profiles. To inspect the opt-in remediation RBAC:

```bash
kubectl kustomize deploy/k8s/overlays/dev-remediation-rbac
```

Apply it only for a controlled remediation test with the required Gateway approval, audit, preflight, execution lock, post-check, and rollback-required guardrails:

```bash
kubectl apply -k deploy/k8s/overlays/dev-remediation-rbac
```

Then explicitly enable mutation execution on the already deployed connector for that test window:

```bash
kubectl -n aiops-dev set env deploy/aiops-connector AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION=true
kubectl -n aiops-dev rollout status deploy/aiops-connector --timeout=180s
```

When the test ends, close both gates:

```bash
kubectl -n aiops-dev set env deploy/aiops-connector AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION=false
kubectl delete -k deploy/k8s/overlays/dev-remediation-rbac
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

Before applying `dev-external`, set `AIOPS_NAMESPACE_SCOPE` to the namespaces you want to diagnose and point `PROMETHEUS_URL`/`LOKI_URL` at backends that actually have data for them. See the in-file comments in `overlays/dev-external/kustomization.yaml`.

After applying `dev-external`, restart diagnosis service if the ConfigMap changed and verify
the live pod env before running the Alertmanager smoke. The rendered manifest is
not enough because existing pods keep their old process env:

```bash
kubectl -n aiops-dev rollout restart deploy/aiops-diagnosis
kubectl -n aiops-dev rollout status deploy/aiops-diagnosis --timeout=180s
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- sh -c 'echo $AIOPS_DIAGNOSIS_TOOL_TIMEOUT_SECONDS $AIOPS_NAMESPACE_SCOPE'
```

For the current `dev-external` PodCrashLooping smoke this must print `30
demo-apps`. If it prints an empty timeout, diagnosis service will fall back to the 3s model
tool timeout and can return avoidable `partial` evidence.

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

When switching from `dev-bundled` to `rc-bundled-digest`, existing Deployments and Services are updated in place. Kubernetes Job pod templates are immutable, so the RC overlay does not mutate retained default or previous RC Jobs. It creates a head-scoped Job `aiops-loki-synthetic-log-rc-454bd0c` with the pinned Loki digest. If the old default Job is no longer needed, delete it explicitly:

```bash
kubectl -n aiops-dev delete job aiops-loki-synthetic-log
```

## Verify

Wait for the core split services:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-gateway --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-connector --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-diagnosis --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-mcp-prometheus --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-mcp-loki --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-mcp-topology --timeout=180s
```

Check health/readiness from the Diagnosis Pod, which is an allowed internal caller:

```bash
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://aiops-gateway:8080/healthz', timeout=5).read().decode()); print(urllib.request.urlopen('http://aiops-connector:8081/healthz', timeout=5).read().decode()); print(urllib.request.urlopen('http://127.0.0.1:8082/readyz', timeout=5).read().decode()); print(urllib.request.urlopen('http://aiops-mcp-topology:8085/readyz', timeout=5).read().decode())"
```

在 Console `/admin` 的 Connector tab 中使用 ConfigMap 已声明的 `AIOPS_CONNECTOR_ID` 与 `AIOPS_CLUSTER_ID` 创建 Enrollment，把一次性返回的 credential 写入 Connector 独占的 Secret 后重启 Connector：

```bash
kubectl -n aiops-dev create secret generic aiops-connector-secret \
  --from-literal=AIOPS_CONNECTOR_CREDENTIAL='<credential-returned-by-connector-enrollment>' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aiops-dev rollout restart deploy/aiops-connector
```

Enrollment 本身不会创建 Cluster；Connector 首次成功注册后，Cluster tab 才会出现该 Cluster。

Check Gateway/Connector registration:

```bash
kubectl -n aiops-dev get deploy/aiops-connector
kubectl -n aiops-dev get pods -l app.kubernetes.io/name=aiops-connector
```

Bundled Prometheus evidence:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-dev-prometheus --timeout=180s
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import json, urllib.request; from apps.internal_auth import internal_auth_headers; payload={'request_id':'prom-smoke','cluster_id':'dev-bundled','reason':'k8s bundled smoke','query':'up','max_series':5}; req=urllib.request.Request('http://aiops-mcp-prometheus:8083/query_metrics', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',**internal_auth_headers()}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

Bundled Loki evidence:

```bash
kubectl -n aiops-dev rollout status deploy/aiops-dev-loki --timeout=180s
kubectl -n aiops-dev wait --for=condition=complete job/aiops-loki-synthetic-log --timeout=120s
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import json, urllib.request; from apps.internal_auth import internal_auth_headers; payload={'request_id':'loki-smoke','cluster_id':'dev-bundled','reason':'k8s bundled smoke','query':'{app=\"payment-api\"}','time_range':{'type':'relative','value':'15m'},'max_lines':20}; req=urllib.request.Request('http://aiops-mcp-loki:8084/query_logs', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',**internal_auth_headers()}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

Topology missing-data evidence stays structured and does not fabricate an evidence ref:

```bash
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import json, urllib.request; from apps.internal_auth import internal_auth_headers; payload={'request_id':'topology-missing-smoke','cluster_id':'dev-bundled','namespace':'aiops-dev','service':'missing-api'}; req=urllib.request.Request('http://aiops-mcp-topology:8085/get_service_topology', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',**internal_auth_headers()}, method='POST'); body=urllib.request.urlopen(req, timeout=10).read().decode(); print(body); assert 'service_not_found' in body"
```

For RC digest-pinned validation, wait on the head-scoped RC Job name and use the RC cluster id:

```bash
kubectl -n aiops-dev wait --for=condition=complete job/aiops-loki-synthetic-log-rc-454bd0c --timeout=120s
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import json, urllib.request; from apps.internal_auth import internal_auth_headers; payload={'request_id':'loki-rc-smoke','cluster_id':'rc-bundled-digest','reason':'k8s rc digest smoke','query':'{app=\"payment-api\"}','time_range':{'type':'relative','value':'15m'},'max_lines':20}; req=urllib.request.Request('http://aiops-mcp-loki:8084/query_logs', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',**internal_auth_headers()}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```

Disabled profile controlled degradation:

```bash
kubectl -n aiops-dev exec deploy/aiops-diagnosis -- python3 -c "import json, urllib.request; from apps.internal_auth import internal_auth_headers; payload={'request_id':'disabled-prom','cluster_id':'dev-disabled','reason':'disabled smoke','query':'up'}; req=urllib.request.Request('http://aiops-mcp-prometheus:8083/query_metrics', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json',**internal_auth_headers()}, method='POST'); body=urllib.request.urlopen(req, timeout=10).read().decode(); print(body); assert 'backend_unavailable' in body"
```

## Retained Resources

For development validation requested in AIO-71, do not clean up the namespace after smoke. Leave these resources for inspection:

- namespace `aiops-dev`
- core Deployments and Services for Gateway, Connector, Diagnosis, MCP Prometheus, MCP Loki
- Topology MCP Deployment and Service
- PVC `aiops-diagnosis-data`
- bundled profile Deployments and Services for Prometheus, Loki, and `payment-api`
- Job `aiops-loki-synthetic-log`
- RC digest overlay Job `aiops-loki-synthetic-log-rc-454bd0c` when `overlays/rc-bundled-digest` has been applied

Manual cleanup, when explicitly requested:

```bash
kubectl delete -k deploy/k8s/overlays/dev-bundled
```

## Alertmanager Target

Alertmanager must target the split Gateway through an external HTTPS endpoint. Replace the example host in `alertmanager/aiops-alertmanager-route.yaml` with the deployed Gateway host before applying it:

```text
https://aiops.example.com/webhooks/alertmanager
```

Gateway validates `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` when it is configured. Alertmanager can send this with native bearer auth from a Secret reference. Gateway still supports the older optional `ALERTMANAGER_WEBHOOK_SECRET` / `AIOPS_ALERTMANAGER_WEBHOOK_SECRET` HMAC path for manual callers that can sign request bodies, but automatic Alertmanager routing uses bearer auth because Alertmanager generic webhooks cannot compute a body-bound HMAC signature.

Create the matching token in the monitoring namespace where the `AlertmanagerConfig` lives:

```bash
kubectl -n loki create secret generic aiops-alertmanager-webhook \
  --from-literal=token='<opaque-alertmanager-webhook-token>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

Enable the low-noise automatic route:

```bash
kubectl apply -f deploy/k8s/alertmanager/aiops-alertmanager-route.yaml
```

The example route only forwards alerts labeled `aiops_route="gateway"`. Add that label to a test alerting rule, or remove/adjust the matcher when you intentionally want broader automatic routing. Disable automatic routing with:

```bash
kubectl delete -f deploy/k8s/alertmanager/aiops-alertmanager-route.yaml
```

Gateway extracts alert fields, creates or reuses the incident record, writes timeline audit events, and triggers diagnosis through `AIOPS_DIAGNOSIS_URL` + `AIOPS_DIAGNOSIS_PATH`. Root-cause diagnosis remains in the diagnosis service; Gateway only performs the handoff.

HTTPS ingress smoke after applying a dev or RC overlay and configuring the external endpoint:

```bash
kubectl -n aiops-dev run aiops-alertmanager-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest \
  --env=AIOPS_ALERTMANAGER_WEBHOOK_TOKEN='<opaque-alertmanager-webhook-token>' \
  --command -- python3 -c "import json, os, urllib.request; payload={'alerts':[{'status':'firing','labels':{'alertname':'PodCrashLooping','severity':'critical','namespace':'default','cluster':'dev-cluster','aiops_route':'gateway'},'annotations':{'description':'pod restart count is increasing'}}]}; headers={'Content-Type':'application/json'}; token=os.environ.get('AIOPS_ALERTMANAGER_WEBHOOK_TOKEN','').strip(); headers.update({'Authorization':'Bearer '+token} if token else {}); req=urllib.request.Request('https://aiops.example.com/webhooks/alertmanager', data=json.dumps(payload).encode(), headers=headers, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode())"
```
