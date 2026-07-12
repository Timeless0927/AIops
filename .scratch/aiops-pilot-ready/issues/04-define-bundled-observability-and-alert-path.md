Type: research
Status: resolved
Blocked by: 01 (Establish Current Pilot Baseline)

# Define Bundled Observability And Alert Path

## Question

What minimal real Prometheus, Alertmanager, and Loki deployment and collection path should the single-Cluster Pilot bundle so that Kubernetes workload metrics, logs, alert evaluation, Alert Signal ingress, and Diagnosis evidence all use real data rather than compatibility responses or pre-seeded artifacts?

The answer must identify current manifest reuse, missing collectors/rules/routes, resource and retention defaults, namespace/RBAC boundaries, health checks, and the clean extension point for later external observability backends.

## Answer

Pilot Release Bundle 在固定 `aiops-system` namespace 内自包含真实 Prometheus、Alertmanager、single-binary Loki、kube-state-metrics 和 Grafana Alloy。它不依赖 Helm、Prometheus Operator CRD 或 `/root/aiops/monitor` 目录；实现时把该目录已跑过的 Loki、Alloy 和 resource configuration 语义迁入 versioned Kustomize manifest，并以公开 immutable image digest 固定。

```text
Kubernetes objects -> kube-state-metrics -> Prometheus -> Alertmanager -> Gateway
Kubernetes pod logs ----------------Alloy -> Loki
Prometheus <- Prometheus MCP <- Diagnosis -> Loki MCP -> Loki
```

### Canonical bundle

| Workload | Shape | Pilot default |
| --- | --- | --- |
| Prometheus | single-replica `Deployment`, `strategy: Recreate`, ClusterIP Service, native ConfigMap/rule files | `15s` scrape/evaluation；`10Gi` RWO PVC；retention `7d` 与 `8GB` size cap；request `500m/1Gi`、limit `1 CPU/2Gi` |
| Alertmanager | single-replica `Deployment`, ClusterIP Service, native ConfigMap, `emptyDir` | retention `48h`；request `50m/128Mi`、limit `100m/256Mi`；restart 后允许因重新发送 firing alert 产生幂等重复，不新增第七个 PVC |
| Loki | single-binary `Deployment`, `strategy: Recreate`, ClusterIP Service | `10Gi` RWO PVC；filesystem + TSDB v13、24h index、replication 1、compactor physical deletion、retention `168h`；request `500m/512Mi`、limit `1 CPU/1Gi` |
| kube-state-metrics | single-replica `Deployment`, ClusterIP Service | 只启用 Pilot 所需 workload collectors；request `50m/64Mi`、limit `100m/128Mi`；无持久化 |
| Alloy | per-node `DaemonSet` | 复用 `/root/aiops/monitor` 已跑过的 `spec.nodeName` partition、Pod discovery 和 `namespace/pod/container/app/job` relabel；request `50m/128Mi`、limit `300m/256Mi`；直接写 Loki |

不部署 node-exporter、Grafana、Tempo、Pyroscope、OpenTelemetry Collector、Alloy aggregator、remote-write、HA replica、MinIO、memcached 或 Loki gateway。需要 node hardware metrics、trace/profile、对象存储或更大规模日志采集时另开需求，不为 Pilot 预留第二套 provider abstraction。

### Collection and alert path

Prometheus 直接抓取自身、Alertmanager、Loki、kube-state-metrics、七个 AIOps `/metrics` 和显式标注 `prometheus.io/scrape=true` 的 workload Pod。现有 `ServiceMonitor` 与 `PrometheusRule` 只复用 scrape/rule 意图；规则转换成 Prometheus 原生 rule file，Operator CRD manifest 从 canonical base 移到可选 external integration，不参与 clean install。

现有 control-plane unavailable、durable work stalled、Unknown Outcome、storage pressure 和 Notification dead-letter 规则按真实 target labels 修正后加载。06 另提供一条只覆盖 `aiops-verification` fixture 的 workload unavailable rule。任何送入 Gateway 的 rule 必须产生 exact `cluster`、`namespace`、workload identity、`service`、`severity` 和 `aiops_route="gateway"`；single-Cluster bundle 的 `AIOPS_CLUSTER_ID` 同时作为 Prometheus `external_labels.cluster` 和 Connector Enrollment 要求的 identity，不允许 Gateway 猜测 Cluster。

Prometheus 把 alerts 发送给 bundled Alertmanager。Alertmanager root receiver 丢弃无 `aiops_route=gateway` 的 alert，matching child route 使用 `group_by: [cluster, namespace, alertname]`、`group_wait: 5s`、`send_resolved: true`，通过 ClusterIP HTTP 调用 Gateway `/webhooks/alertmanager`。Bearer credential 从 bootstrap `aiops-runtime-secret` 只投影 `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` key；不挂载其他 Secret data。

Alloy 使用 Kubernetes API 发现并 tail 每个 node 上的真实 Pod log，不使用 synthetic push Job。只保留 bounded identity labels，不默认删除 DEBUG/TRACE 或其他业务日志；Diagnosis 的 query guard 和 result limit 继续负责读取边界。

### Ownership and security

Prometheus、kube-state-metrics 和 Alloy 使用独立 ServiceAccount 与最小 read-only ClusterRole。Prometheus 只取得 Pod discovery 的 `get/list/watch`；kube-state-metrics 只 list/watch selected workload kinds；Alloy 只取得 Pod discovery 与 `pods/log` read。它们不得读取 Secret/ConfigMap 内容或拥有 write verb。Loki 与 Alertmanager 不访问 Kubernetes API，并禁用自动 token mount。

NetworkPolicy 只允许 Prometheus MCP 查询 Prometheus、Loki MCP 查询 Loki、Alloy push Loki、Prometheus scrape/调用 Alertmanager，以及 Alertmanager 调 Gateway。Observability backend 不创建 NodePort/Ingress，浏览器仍只访问 Console/Gateway。Pilot 明确接受对一个真实非生产 Cluster 的 cluster-wide workload state/log read；不能接受该读取面的组织应使用后续 external backend profile，而不是 synthetic fallback。

### Health and evidence

Kubernetes probe 使用原生 endpoint：Prometheus 与 Alertmanager `/-/healthy`、`/-/ready`，Loki `/ready`，Alloy `/-/ready`，kube-state-metrics `/livez`、`/readyz`。Probe 只证明进程可服务，不能标记 observability verified。

Operational evidence 必须同时证明 Prometheus targets up、真实 workload series 可查询、rule 已加载并求值、Loki 可按 run ID 查到 workload stdout/stderr、Alertmanager 收到真实 firing/resolved alert、Gateway 产生对应 Alert Signal/Incident/Recovery Observation，以及 Diagnosis 经 Prometheus/Loki MCP 得到真实 evidence refs。直接 backend curl 仅用于定位故障，不能替代 MCP/Diagnosis 产品边界。

当前 Python Prometheus/Loki compatibility handlers、in-memory `LINES`、constant `payment-api` metric、synthetic Loki push Job、fake runner tests、手工 POST Alertmanager JSON、仅 apply CRD、空 query 或 `/ready` 200 均不能计入 Pilot acceptance。实现真实 bundle 时删除这些 deployment happy-path；contract tests 可保留 fake Adapter。

### Existing monitor reuse and external extension

从 `/root/aiops/monitor` 迁入 Loki single-binary/TSDB/filesystem/compactor/7-day retention、Alloy node partition/relabel，以及 Prometheus/Loki/Alertmanager 已跑过的资源量级。不得迁入 Helm archives、Prometheus Operator/admission webhook、固定 `loki`/`middleware` namespace DNS、private Harbor/tag、`local-path`、`ops=test` scheduling、20Gi PVC、hostPort/hostPath、wide Secret/CRD RBAC、aggregator、trace/profile pipeline 或业务日志过滤。该外部目录是研究输入，不是 release runtime/build dependency。

后续 external profile 的唯一 extension point 继续是 MCP-owned `PROMETHEUS_URL` 与 `LOKI_URL` 及 Gateway Alertmanager webhook contract：external profile 不部署 bundled Prometheus/Loki/kube-state-metrics/Alloy，向现有 HTTP runners 注入 endpoint/TLS/credential，并保持 query guard、ToolEnvelope、evidence ref、cluster/rule labels、bearer token 和 `send_resolved` 不变。只有出现第二个真实认证/tenant Adapter 时才建立新的 code seam；当前不增加 registry、plugin 或 failover。

## Research

### 结论

Pilot Release Bundle 应在固定的 `aiops-system` namespace 内部署真实、单副本的 Prometheus、Alertmanager、Loki、kube-state-metrics，以及 per-node Grafana Alloy，并继续让 Diagnosis 只通过现有 Prometheus MCP 与 Loki MCP 读取 evidence：

```text
Kubernetes API -> kube-state-metrics -> Prometheus -> Alertmanager -> Gateway /webhooks/alertmanager
       |                                  |
       +-> Alloy -> Loki                  +-> Prometheus MCP -> Diagnosis
                         Loki MCP ---------------------------> Diagnosis
```

这是当前需求所需的最小链路。kube-state-metrics 提供 Deployment、Pod、ReplicaSet 等真实 Kubernetes 对象状态；Alloy 复用 `/root/aiops/monitor` 已跑过的 DaemonSet node partition，以 `loki.source.kubernetes` 从 Kubernetes API tail 真实 container log，但删除无用 hostPath/hostPort。Promtail 已于 2026-03-02 EOL，不应进入新 bundle。[Alloy `loki.source.kubernetes`](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.kubernetes/)，[Kubernetes cluster-level logging](https://kubernetes.io/docs/concepts/cluster-administration/logging/)，[Promtail lifecycle](https://grafana.com/docs/loki/latest/send-data/promtail/)

不加入 node-exporter、OpenTelemetry Collector、Grafana UI、remote-write、HA replica 或另一套 provider abstraction。Pilot 的受控 workload unavailable/restart 场景只需要 Kubernetes workload state、container log、AIOps service metrics、规则求值和告警投递；需要 node hardware metrics 或生产容量时再增加 collector。

### 现状与可复用部分

当前 `deploy/k8s/bundled/observability-bundled.yaml` 不能迁入 Pilot bundle：

- 名为 Prometheus 的 Deployment 实际用 MCP image 启动内联 Python `http.server`，固定返回 `up=1`；虽然 ConfigMap 含 scrape config，该进程不运行 Prometheus，也不读取该配置（`deploy/k8s/bundled/observability-bundled.yaml:49-74,97-138`）。
- 名为 Loki 的 Deployment 是进程内 list 加 HTTP compatibility handler，启动时预置 synthetic log，push 解析失败也被吞掉（同文件 `:198-256`）。
- `payment-api` 只返回常量 metric，`aiops-loki-synthetic-log` Job 直接向 fake Loki push 构造日志（同文件 `:289-416`）。部署文档也明确称这些为 inline Python compatibility handlers，而非 production server binary（`deploy/k8s/README.md:82`）。
- manifest test 只断言这些对象和 synthetic 字符串存在，不证明 scrape、tail、rule evaluation 或 delivery（`tests/test_k8s_manifests.py:445-460`）；当前功能矩阵的 Prometheus/Loki coverage 使用 fake runner/backend（`docs/v1-functional-test-matrix.md:9-14`）。

应复用的是真实产品边界，而不是上述 manifest：

- Prometheus MCP 已把 bounded `query_range` 交给 `PROMETHEUS_URL`，并把 timeout/backend failure 变成受控错误（`toolsets/prometheus_query.py:53-95,306-346`）。Loki MCP 同样只依赖 Loki HTTP `query_range`（`toolsets/loki_query.py:53-101`）。
- Diagnosis 只调用 MCP 的 `query_metrics`/`query_logs`；未配置或调用失败形成 partial/failed ToolEnvelope，不造 evidence ref（`diagnosis_service/service_main.py:183-236,264-286`）。
- Gateway Alertmanager Adapter 已校验 bearer/HMAC、payload 和 Alert Signal 字段，并 fail closed（`apps/aiops_k8s_gateway/alertmanager_webhook.py:25-48,80-159`）。Alert Signal 的 `cluster` 必须对应已注册 Cluster，否则 Gateway 拒绝 `cluster_not_registered`（`apps/aiops_k8s_gateway/incident.py:180-188`）。
- 七个 AIOps Service 已有 `/metrics`，现有 ServiceMonitor 和两份 PrometheusRule 可复用 scrape/rule **意图**（`deploy/k8s/base/servicemonitor.yaml:12-34`，`deploy/k8s/base/control-plane-prometheusrule.yaml:11-40`，`deploy/k8s/base/notification-prometheusrule.yaml:10-19`）。canonical bundle 不得直接包含这些 CRD，因为 02 已固定不依赖 Prometheus Operator（`.scratch/aiops-pilot-ready/issues/02-define-kustomize-release-installation-contract.md:48`）；应把 scrape job 和规则移入原生 Prometheus ConfigMap/rule file。

现有 `AlertmanagerConfig` 也只能作为外部 Operator 集成参考。它硬编码 `loki` namespace、示例公网 URL，并要求手工创建 Secret 和单独 apply（`deploy/k8s/alertmanager/aiops-alertmanager-route.yaml:1-32`，`deploy/k8s/README.md:379-406`）。bundled Alertmanager 与 Gateway 同 Cluster，应直接访问 `http://aiops-gateway:8080/webhooks/alertmanager`。

### Canonical bundle 对象与配置

所有下列 OCI image 都必须按 02 的 release contract 固定完整 `@sha256` digest；release 内保存本地 manifest/config，不引用 remote Kustomize base。

| Module | 最小 Kubernetes 对象 | 必需配置 |
| --- | --- | --- |
| Prometheus | `ServiceAccount`、read-only `ClusterRole/Binding`、ConfigMap、`Deployment`、Service、`10Gi` RWO PVC | `15s` scrape/evaluation interval；抓取自身、Alertmanager、Loki、kube-state-metrics、七个 AIOps `/metrics` 和显式标注的 workload Pod；加载本地 rule file；发送到 bundled Alertmanager；`external_labels.cluster` 固定为 Pilot setup 使用的 exact `cluster_id`。 |
| Alertmanager | 独立无 Kubernetes API 权限的 `ServiceAccount`、ConfigMap、`Deployment`、Service、只投影 webhook token key 的 Secret volume | root receiver 丢弃非 AIOps alert；`aiops_route=gateway` child receiver 指向 Gateway ClusterIP；`send_resolved: true`；`authorization.credentials_file` 读取 bootstrap 生成的 `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN`；不依赖 `AlertmanagerConfig`。Alertmanager 原生 webhook/http auth 配置见[官方 configuration](https://prometheus.io/docs/alerting/latest/configuration/)。 |
| Loki | `automountServiceAccountToken: false` 的 ServiceAccount、ConfigMap、单 binary `Deployment`、Service、`10Gi` RWO PVC | `auth_enabled: false`，仅 ClusterIP/NetworkPolicy 可达；TSDB schema v13、filesystem object store、single binary target、compactor retention。官方 single-store/filesystem 与配置边界见[Loki storage](https://grafana.com/docs/loki/latest/configure/storage/)和[configuration examples](https://grafana.com/docs/loki/latest/configure/examples/configuration-examples/)。 |
| kube-state-metrics | 收敛后的 upstream `ServiceAccount`、read-only `ClusterRole/Binding`、`Deployment`、Service | 只启用 Pilot 需要的 `pods,deployments,replicasets,statefulsets,daemonsets,jobs,namespaces` collectors；不采 Secret/ConfigMap 内容或增加 Kubernetes write。以 upstream standard manifests 为起点后 vendor 到 release：[kube-state-metrics standard examples](https://github.com/kubernetes/kube-state-metrics/tree/main/examples/standard)。 |
| Alloy | `ServiceAccount`、read-only `ClusterRole/Binding`、ConfigMap、per-node `DaemonSet` | `discovery.kubernetes` 以 `spec.nodeName` partition 发现 Pod，`loki.source.kubernetes` 读取 `pods/log`，relabel 出 bounded `cluster/namespace/pod/container/app` labels，写 bundled Loki；不挂 host filesystem。发现机制见[Alloy Kubernetes discovery](https://grafana.com/docs/alloy/latest/reference/components/discovery/discovery.kubernetes/)。 |

Prometheus 与 Loki 使用 `strategy: Recreate`，避免单副本 RWO PVC 在 rollout 时出现两个 Pod 争用；Alertmanager、kube-state-metrics 使用普通单副本 Deployment，Alloy 使用 DaemonSet。02 已固定 namespace `aiops-system`、Prometheus/Loki 各 `10Gi` PVC、默认 StorageClass 和至少 `32Gi` Cluster 可用容量（`.scratch/aiops-pilot-ready/issues/02-define-kustomize-release-installation-contract.md:32-48,65-78`），本票不增加第七个 PVC。

### Scrape、规则与 route

Prometheus 原生 config 至少包含：

1. stable DNS/static target 抓取 Prometheus、Alertmanager、Loki、kube-state-metrics 和七个 AIOps Service；AIOps Service 已统一使用 named `http` port 和 `/metrics`（`deploy/k8s/base/service.yaml:1-111`，`deploy/k8s/base/servicemonitor.yaml:12-34`）。
2. Kubernetes Pod discovery 抓取显式 `prometheus.io/scrape=true` 的 workload；保留 `namespace/pod/container/app` 等 bounded labels，不把 Incident/User/Connector identity 变为 metric label。
3. 将现有 control-plane unavailable、durable work stalled、Unknown Outcome、storage pressure、Notification dead-letter 表达式移入原生 rule file，但按实际 static job/label 修正 target selector；原 CRD 目前依赖不存在的 Operator selector，不能原样宣称 active。
4. 增加一条只覆盖 `aiops-verification` fixture 的真实 workload rule，例如以 `kube_deployment_status_replicas_unavailable > 0` 判断 exact verification Deployment unavailable。rule 必须保留 `namespace`、`deployment`，并添加 `severity`、`aiops_route="gateway"` 和 `service`；`cluster` 由 Prometheus `external_labels` 注入。

该 rule 由 Prometheus 实际 evaluate，Alertmanager 以自己的 fingerprint 发 firing/resolved webhook。Gateway parser 从 payload 读取 `cluster`、`namespace`、`deployment`、`service` 和 severity（`apps/aiops_k8s_gateway/alertmanager_webhook.py:25-67`），因此 Web Setup 创建 Enrollment 时必须使用与 bundle `external_labels.cluster` 相同的 `cluster_id`；不允许用空 label、另一个 ID 或 Gateway 猜测唯一 Cluster。Prometheus rule file、alerting target 和 Kubernetes discovery 均是原生配置能力，无需 Operator CRD：[Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)，[alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)。

Alertmanager route 使用短 `group_wait`（Pilot 默认 `5s`）、`group_by: [cluster, namespace, alertname]` 和 `send_resolved: true`。它只接收带 `aiops_route="gateway"` 的 AIOps-owned rules，避免把 Cluster 中不相关的全部告警自动转成 Incident；后续接外部 Alertmanager 时沿用同一 label/webhook/token contract。

### Namespace、RBAC 与网络边界

- Prometheus、kube-state-metrics 和 Alloy 各用独立 ServiceAccount，不复用 Connector 或 AIOps process identity。Prometheus 只有 workload Pod discovery 所需的 cluster-wide `get/list/watch`；kube-state-metrics 只有所选 object kinds 的 `list/watch`；Alloy 只有 Pod discovery 的 `get/list/watch` 和 `pods/log get`。三者没有 Secret read 或任何 write verb。
- Loki 与 Alertmanager 不访问 Kubernetes API，禁用自动挂载 ServiceAccount token。Alertmanager 只挂载 runtime Secret 中 webhook token 这一项，不挂 admin password 或 encryption key。
- NetworkPolicy 只允许 Prometheus MCP 查询 Prometheus、Loki MCP 查询 Loki、Alloy push Loki、Prometheus scrape bundled components/接入 Alertmanager、Alertmanager 调 Gateway。Prometheus、Loki、Alertmanager 不创建 NodePort/Ingress，浏览器仍只访问 Console/Gateway，符合 `docs/current-architecture.md:18-24`。
- cluster-wide logs 可能含应用敏感内容；Pilot 只用于真实非生产 Cluster，Loki 不能对浏览器或外部网络开放，Diagnosis 仍经过现有 LogQL query guard、line/window/byte limits。若组织不能接受这一读取面，应选择后续 external backend profile，而不是静默返回 synthetic log。

Kubernetes RBAC 是显式 allow rule，资源 request/limit 分别参与调度与 runtime enforcement；bundle 应声明两者，不能继续使用当前完全空白的 resource stanza。[Kubernetes RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)，[container resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

### Pilot 默认资源与保留

这些是小型、单 Cluster Pilot 的保守起点，不是容量承诺；超过 limit、持续 storage pressure 或查询延迟门限后再根据真实数据调高。

| Workload | CPU request / limit | Memory request / limit | Storage / retention |
| --- | ---: | ---: | --- |
| Prometheus | `500m / 1` | `1Gi / 2Gi` | `10Gi` PVC；`--storage.tsdb.retention.time=7d` 且 `--storage.tsdb.retention.size=8GB`，任一先到即删除旧 block。Prometheus 默认 15d，但明确双边界更适合固定 Pilot PVC；行为见[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/)。 |
| Loki | `500m / 1` | `512Mi / 1Gi` | `10Gi` PVC；`limits_config.retention_period: 168h`，compactor retention enabled，TSDB index period `24h`。Loki retention 由 compactor 执行且要求 24h index period，见[官方 retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)。 |
| Alertmanager | `50m / 100m` | `128Mi / 256Mi` | `emptyDir`；`--data.retention=48h`。Pod replacement 会丢 silences/dedup state，Prometheus 会重新发送仍 firing 的 alert；Pilot 不承诺 Alertmanager durable silence。出现真实 silence/dedup persistence 要求时再增加 PVC，并同步修改 02 的 storage contract。 |
| kube-state-metrics | `50m / 100m` | `64Mi / 128Mi` | 无持久化。 |
| Alloy | `50m / 300m` per node | `128Mi / 256Mi` per node | 无持久化；API/kubelet load 或采集 lag 超限时再评估 node-local file collector。 |

Loki 只有 time retention、没有可靠的本地 filesystem size cap，因此同时保留现有 AIOps storage pressure alert意图并监测 PVC available ratio；`7d` 不代表任意日志量都能装入 `10Gi`。Loki 原始日志保留与 Gateway governance history 分离，符合 `docs/current-architecture.md:71`。

### Health 与可验证边界

Kubernetes probe 使用后端原生 endpoint：Prometheus `/-/healthy` 与 `/-/ready`，Alertmanager `/-/healthy` 与 `/-/ready`，Loki `/ready`，Alloy `/-/ready`，kube-state-metrics 使用 upstream manifest 的 `/livez` 与 `/readyz`。Prometheus/Alertmanager endpoint 语义见各自[Prometheus management API](https://prometheus.io/docs/prometheus/latest/management_api/)和[Alertmanager management API](https://prometheus.io/docs/alerting/latest/management_api/)；Loki query/readiness API 见[Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)，Alloy endpoint 见[Alloy HTTP endpoints](https://grafana.com/docs/alloy/latest/reference/http/)。

probe 成功只证明进程可服务。现有 MCP `/readyz` 无条件返回 200，不检查 backend（`apps/observability_http.py:49-78`）；02 也已区分 Installation Ready 与 observability query readiness（`.scratch/aiops-pilot-ready/issues/02-define-kustomize-release-installation-contract.md:104-113`）。因此完整 operational/acceptance check 必须同时观察：

- Prometheus target API 中 kube-state-metrics 和 AIOps targets 为 up，query API 返回 verification Deployment 的真实 series，rule API 显示该 rule 已加载/求值；
- Loki query API 能按 `cluster/namespace/pod/container` 找到 verification workload 自己写到 stdout/stderr 的唯一 run ID，不接受 push helper 注入；
- 通过 Prometheus rule 进入 Alertmanager 的 firing alert 出现在 Alertmanager API，Gateway 公开 API 出现对应 Alert Signal/Incident；恢复后同 fingerprint 的 resolved webhook 形成 Recovery Observation；
- Diagnosis 经两个 MCP public Interface 取得真实 series/log stream 和 evidence refs。直接 curl backend 只能证明 backend，不能替代 MCP/Diagnosis 边界。

这些 checks 属于 06 的 controlled scenario 和 08 的 acceptance matrix；本票不新增周期 health worker、持久化 readiness 状态或第二套告警状态机。

### External backend extension point

外部 backend 的稳定 extension point 已存在于 MCP owner：`PROMETHEUS_URL` 和 `LOKI_URL`。`dev-external` 已仅通过替换这两个 URL 改后端（`deploy/k8s/overlays/dev-external/kustomization.yaml:8-22`），Diagnosis contract 无需变化。后续 external profile 应：

1. 不部署 bundled Prometheus/Loki/kube-state-metrics/Alloy；
2. 给现有 `HttpPrometheusRunner`/`HttpLokiRunner` 注入 endpoint、TLS 和 credential configuration；
3. 保持 MCP request/ToolEnvelope/evidence ref、query guard 和 Gateway Alertmanager webhook contract 不变；
4. 由外部 Prometheus/Alertmanager 保证同一 `cluster` label、AIOps rule labels、bearer token 和 `send_resolved`。

当前不创建 backend registry、plugin interface 或 failover。只有出现第二个真实认证/tenant Adapter 时，才在 MCP 内把 HTTP transport 配置提升为 owner-held backend configuration；浏览器、Gateway 和 Diagnosis 均不得直连 observability backend。

### 明确不能计入 Pilot acceptance 的路径

- `deploy/k8s/bundled/observability-bundled.yaml` 的 Python Prometheus/Loki compatibility response、内存 `LINES`、常量 `payment-api` metrics 和 synthetic push Job；
- `tests/test_prometheus_query_facade.py:17-27`、`tests/test_loki_query_facade.py:15-25` 等 fake runner/monkeypatch 测试；它们保留为 contract test，但不是 deployed evidence；
- `deploy/k8s/smoke/pod-crashlooping-smoke.yaml:5-25` 仅给 Pod 加 `alertname` label；label 本身不会产生 Prometheus rule evaluation；
- `deploy/k8s/README.md:411-417` 手工构造 Alertmanager JSON 直接 POST Gateway；它绕过 scrape、rule evaluation 和 Alertmanager；
- 只 render/apply `ServiceMonitor`、`PrometheusRule` 或 `AlertmanagerConfig`，却没有实际 Operator selector、loaded rule/target/alert 证据；
- backend `/ready`、MCP `/readyz` 或空 query 的 200 response；
- pre-seeded fixture、direct SQLite write、fake model/keyword fallback，或 synthetic product response。Baseline 已把这些与真实部署行为分开（`.scratch/aiops-pilot-ready/issues/01-establish-current-pilot-baseline.md:18-30`），05 也禁止把 keyword diagnosis 当 model diagnosis（`.scratch/aiops-pilot-ready/issues/05-define-model-and-notification-readiness.md:84-88`）。

### Existing /root/aiops/monitor reuse audit

`/root/aiops/monitor` 是一套已调优 Helm values，不是可直接纳入 02 所定义自包含 Kustomize bundle 的 manifest。应复用其中已跑过的核心配置，重新落成 `aiops-system` native objects，而不是重新设计或把 Helm/Operator 变成安装前提。

**可直接搬配置语义：**

- Loki 已验证的最小形态与本票一致：single-binary、`auth_enabled: false`、replication 1、TSDB v13、filesystem、24h index（`/root/aiops/monitor/loki/loki-lean-values.yml:1-25`），以及 `168h` retention、compactor physical deletion 和 filesystem directories（同文件 `:57-82`）。关闭 gateway、memcached、MinIO、canary 和 read/write/backend replica 的精简开关也可保留（同文件 `:84-142`）；只把 `20Gi/local-path` 改成 02 已定的 `10Gi` 默认 StorageClass（`:130-135`）。
- Prometheus 的 `7d` retention 和已实跑资源基线 `500m/1Gi` request、`1 CPU/2Gi` limit 已成为 Answer 默认（`/root/aiops/monitor/prometheus/prometheus-lean-values.yaml:181-227`）；Alertmanager 的单副本、`48h` 和 `50m/128Mi` request、`100m/256Mi` limit 同理（同文件 `:54-79`）。Kube-state-metrics 的启用与 `100m/128Mi` limit 可作上限起点（`:280-304`）。PVC 大小仍服从 02，不照搬 `20Gi`。
- Alloy 的 node-scoped Pod discovery、`namespace/pod/container/app/job` relabel 和 `loki.source.kubernetes` 是最有价值的现成 collector（`/root/aiops/monitor/alloy/alloy-agent-values.yaml:157-210`）。优先复用其 DaemonSet + `spec.nodeName` 分片，而不是另造 collector；但直接写 `loki.aiops-system.svc:3100`，删除只做转发的 aggregator hop。

**必须解除的环境耦合与风险：**

- 所有 `*.loki.svc.cluster.local` 固定 DNS 必须改为 `aiops-system` Service（Prometheus values `:128-174`；Alloy agent `:68-72,149-155,249-260`；Alloy aggregator `:181-199`）。`ServiceMonitor.yml` 全部固定 `middleware` namespace、具体中间件 label，且多处明确写着猜测 port/path 或要求手工 label（`/root/aiops/monitor/ServiceMonitor.yml:1-174`），不能进入 Pilot。
- 私有 `harbor.5gfusion.com` registry、mutable tag、`local-path` StorageClass、`ops=test` nodeSelector/toleration 都是假定当前环境存在的条件（Prometheus values `:56-79,181-227,232-300`；Loki values `:3-9,114-135`）。Release 必须改为公开可拉取的 immutable digest、默认 StorageClass，并删除 node label/toleration 依赖。
- kube-prometheus-stack values 显式启用 Prometheus Operator/admission webhook 和 unrestricted ServiceMonitor/PodMonitor selectors（Prometheus values `:193-203,229-278`），还启用并假定 kubelet/control-plane endpoints 与 TLS 形态（`:12-52`）；这违反 02 的 no-Operator-CRD contract，也不能假设每种 Cluster 都暴露相同 control-plane endpoints。只移动 Prometheus/Alertmanager/kube-state-metrics 必需配置和本项目 rules。
- Alloy agent 暴露 `4040/4317/4318` hostPort 并挂载 host `/var/log`、Docker container directory（Alloy agent `:16-41`），但实际 log source 已通过 Kubernetes API；这些端口和 hostPath 对 Pilot 无用且会制造冲突/额外 node access，应删除。其 RBAC 又读取 Secrets、ConfigMaps、Operator CRD 和 node metrics（`:293-298`），应收敛为 Pod discovery 与 `pods/log` read。
- 不复用删除所有 debug/trace line 的业务特定过滤（Alloy agent `:212-247`），否则真实 evidence 可能在 collector 层永久丢失。也不复用 2-4 CPU、4-8Gi memory 的 OTLP/Tempo/Pyroscope aggregator、remote-write receiver 和 tail-sampling pipeline（`/root/aiops/monitor/alloy/alloy-aggregator-values.yaml:1-35,75-199`）；04 不需要 traces/profiles/host metrics。
- 现有 Alertmanager block 只有运行参数，没有 Gateway receiver、bearer credential、`aiops_route` matcher 或 `send_resolved` route（Prometheus values `:54-80`）。因此该目录证明真实 server/resource tuning，但尚未打通 AIOps Alert Signal ingress；仍需本票定义的 internal Gateway webhook config 和 controlled rule。

该审计确认最终选择：Alloy 采用已跑过的 per-node DaemonSet log partition；Prometheus/Loki/Alertmanager resource request 以现有 tuned values 为默认。其余 canonical boundary 不变：不用 Helm/Operator，不加入 aggregator/Grafana/Tempo/Pyroscope，Diagnosis 仍只经 MCP 查询。
