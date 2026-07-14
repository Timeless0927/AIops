# Pilot 安装

当前 kube context 必须指向目标非生产 Cluster，并具备创建 `aiops-system`、namespaced workload、RBAC 和 ClusterRole 的权限。Cluster 还需提供默认 RWO StorageClass、至少 22Gi 可用动态存储、可用的 NodePort `30088`，并实际执行 NetworkPolicy。所有节点必须能拉取 overlay 中按 digest 固定的公开镜像。

唯一安装入口：

```bash
kubectl apply -k deploy/k8s/pilot
```

等待 Installation Ready：

```bash
kubectl wait -n aiops-system --for=condition=complete job/aiops-bootstrap --timeout=2m
kubectl wait -n aiops-system --for=jsonpath='{.status.phase}'=Bound pvc --all --timeout=10m
kubectl wait -n aiops-system --for=condition=Available deployment --all --timeout=10m
kubectl get secret -n aiops-system aiops-runtime-secret -o jsonpath='{.data.AIOPS_BOOTSTRAP_ADMIN_PASSWORD}' | base64 -d
curl --fail --show-error "http://${NODE_IP}:30088/healthz"
```

Installation Ready 包含真实 Prometheus、Alertmanager 和 kube-state-metrics 进程 Ready，但不代表 targets、rules、告警投递或 MCP 查询已经过真实验收，也不代表 Connector、Model Provider、Loki 或 Notification Destination 已配置并验证。

## O01 真实 metrics 与告警链路验收

Canonical bundle 固定使用 `pilot-cluster` 作为 Connector Cluster identity 和 Prometheus `external_labels.cluster`，并把 MCP 的 `PROMETHEUS_URL` 指向 `http://aiops-prometheus:9090`。不得用 direct backend curl 替代产品边界验收；下面的 opt-in 测试同时检查 target、rule、MCP guarded query、Alertmanager firing/resolved webhook、Gateway Incident 和 Recovery Observation：

```bash
AIOPS_RUN_KUBERNETES_INTEGRATION=1 \
  pytest -q tests/test_pilot_observability_integration.py
```

测试会在 `aiops-system` 临时应用 `deploy/k8s/smoke/aiops-verification-unavailable.yaml`，产生真实 Deployment unavailable metric，恢复后自动删除 fixture。测试可能创建 `connector-pilot` / `pilot-cluster` Enrollment；已有同名已注册 Cluster 时直接复用。定位 backend 问题时可临时查看 Prometheus API：

```bash
kubectl -n aiops-system port-forward service/aiops-prometheus 9090:9090
curl --fail --show-error http://127.0.0.1:9090/api/v1/targets
curl --fail --show-error http://127.0.0.1:9090/api/v1/rules
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
