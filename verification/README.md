# Controlled Verification Fixture

该 fixture 只用于非生产验收，默认不随 Pilot 安装。先确认 Platform Status、Connector、Model、Notification 与 bundled observability 已按验收矩阵就绪，再显式安装：

```bash
kubectl apply -k verification/base
kubectl -n aiops-verification wait --for=condition=Available deployment/verification-api --timeout=2m
kubectl apply -k verification/run
kubectl -n aiops-verification wait --for=condition=complete job/verification-trigger --timeout=2m
```

`run_id` 是 Job controller UID，不由人员输入：

```bash
kubectl -n aiops-verification get job verification-trigger -o jsonpath='{.metadata.uid}{"\n"}'
```

Job 优先读取 `batch.kubernetes.io/controller-uid`；Kubernetes 1.26 只注入 legacy `controller-uid` 时读取后者。两者都是同一 Job controller UID；当 Pilot 最低 Kubernetes 版本保证新版 label 后删除 legacy 回退。

故障只让 `/readyz` 返回 503，`/livez` 始终为 200，因此 kubelet 不会自动重启。恢复必须从 Console Recommendation 显式创建 Change Request，经 Gateway/Connector dry-run、Approval 与 single-use grant，把 Deployment pod-template annotation `aiops.dev/verification-run-id` add/replace 为当前 `run_id`。不要手工删除 Pod、rollout restart 或直接 patch 代替产品治理路径。

每轮结束先删除 Job，下一轮 reapply 会获得新 UID：

```bash
kubectl delete -k verification/run --ignore-not-found
kubectl apply -k verification/run
```

最终 cleanup 只删除 fixture namespace，不会删除 `aiops-system` 或产品治理历史：

```bash
kubectl delete -k verification/run --ignore-not-found
kubectl delete -k verification/base --ignore-not-found
```
