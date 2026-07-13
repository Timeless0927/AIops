# Pilot 安装

当前 kube context 必须指向目标非生产 Cluster，并具备创建 `aiops-system`、namespaced workload、RBAC 和 ClusterRole 的权限。Cluster 还需提供默认 RWO StorageClass、至少 12Gi 可用容量、可用的 NodePort `30088`，并实际执行 NetworkPolicy。

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

Installation Ready 不代表 Connector、Model Provider、Prometheus/Loki 或 Notification Destination 已配置或验证。

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
