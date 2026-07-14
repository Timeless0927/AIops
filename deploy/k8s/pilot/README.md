# Pilot 安装

当前 kube context 必须指向目标非生产 Cluster，并具备创建 `aiops-system`、namespaced workload、RBAC 和 ClusterRole 的权限。Cluster 还需提供默认 RWO StorageClass、至少 32Gi 可用动态存储、可用的 NodePort `30088`，并实际执行 NetworkPolicy。所有节点必须能拉取 overlay 中按 digest 固定的公开镜像。

唯一安装入口：

```bash
kubectl apply -k deploy/k8s/pilot
```

## 构建 source-independent Release

Release Operator 从已通过验收的工作树生成确定性归档；版本必须与 Console package version 一致：

```bash
python3 scripts/build_pilot_release.py --version v0.1.0 --output-dir dist
cd dist
sha256sum -c SHA256SUMS
tar -xzf aiops-pilot-v0.1.0.tar.gz
kubectl apply -k ./aiops-pilot-v0.1.0
```

发布物只有 `aiops-pilot-v0.1.0.tar.gz` 与旁置 `SHA256SUMS`。解压目录本身是唯一安装 overlay，包含本地 manifest、verification overlays、contract/revision inventory 和安装说明；目标环境不需要仓库、Docker、Helm、Node/Python 或本地构建。当前只承诺 clean install 与同版本 reapply。

等待 Installation Ready：

```bash
kubectl wait -n aiops-system --for=condition=complete job/aiops-bootstrap --timeout=2m
kubectl wait -n aiops-system --for=jsonpath='{.status.phase}'=Bound pvc --all --timeout=10m
kubectl wait -n aiops-system --for=condition=Available deployment --all --timeout=10m
kubectl get secret -n aiops-system aiops-runtime-secret -o jsonpath='{.data.AIOPS_BOOTSTRAP_ADMIN_PASSWORD}' | base64 -d
curl --fail --show-error "http://${NODE_IP}:30088/healthz"
```

Installation Ready 包含真实 Prometheus、Alertmanager、kube-state-metrics、Loki 和 Alloy 进程 Ready，但不代表 targets、rules、日志采集、告警投递或 MCP 查询已经过真实验收，也不代表 Connector、Model Provider 或 Notification Destination 已配置并验证。

## O01 真实 metrics 与告警链路验收

Canonical bundle 固定使用 `pilot-cluster` 作为 Connector Cluster identity 和 Prometheus `external_labels.cluster`，并把 MCP 的 `PROMETHEUS_URL` 指向 `http://aiops-prometheus:9090`。不得用 direct backend curl 替代产品边界验收；下面的 opt-in 测试同时检查 target、rule、MCP guarded query、Alertmanager firing/resolved webhook、Gateway Incident 和 Recovery Observation：

```bash
AIOPS_RUN_KUBERNETES_INTEGRATION=1 \
  pytest -q tests/test_pilot_observability_integration.py::test_real_prometheus_alertmanager_gateway_and_mcp_path
```

测试会显式安装 version-matched `verification/base` 与 `verification/run`，以 Kubernetes Job controller UID 触发 latched readiness fault，并在两次不同 UID、真实 metric/log、firing/resolved 和 cleanup 后删除 `aiops-verification`。测试中的 direct annotation patch 只验证 fixture mechanics，不计为 governed Approval 证据；真实 Approval/Grant/Connector recovery 由 acceptance frontier 验收。测试可能创建 `connector-pilot` / `pilot-cluster` Enrollment；已有同名已注册 Cluster 时直接复用。定位 backend 问题时可临时查看 Prometheus API：

```bash
kubectl -n aiops-system port-forward service/aiops-prometheus 9090:9090
curl --fail --show-error http://127.0.0.1:9090/api/v1/targets
curl --fail --show-error http://127.0.0.1:9090/api/v1/rules
```

## O02 真实 Pod log 链路验收

Loki 使用 TSDB v13/filesystem 与 `10Gi` RWO PVC，Alloy 以 per-node DaemonSet 通过 Kubernetes Pod log API 收集 stdout/stderr；MCP 的 `LOKI_URL` 固定为 `http://aiops-loki:3100`。下面的 opt-in 测试创建带唯一 run ID 的受限 Pod，验证 Alloy→Loki→MCP evidence，随后把 Loki scale 到 0 验证 owner unavailable error，并在恢复同一 PVC 后复查原日志：

```bash
AIOPS_RUN_KUBERNETES_INTEGRATION=1 \
  pytest -q tests/test_pilot_observability_integration.py::test_real_alloy_loki_mcp_and_owner_unavailable_path
```

测试会恢复 Loki 为 1 replica 并删除临时 Pod。直接 Loki API 只用于定位 backend，不计入产品边界验收：

```bash
kubectl -n aiops-system port-forward service/aiops-loki 3100:3100
curl --fail --show-error http://127.0.0.1:3100/ready
```

失败时先查看：

```bash
kubectl get pods,pvc,job -n aiops-system
kubectl describe pods,pvc,job -n aiops-system
```

Kubernetes 不会重跑 terminal Bootstrap Job。修复失败原因或需要显式复核 bootstrap state 后，只删除 Job 并重复 apply；不得删除已生成的 Secret：

```bash
kubectl delete job -n aiops-system aiops-bootstrap
kubectl apply -k deploy/k8s/pilot
```
