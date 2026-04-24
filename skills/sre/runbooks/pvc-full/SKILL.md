# PVC 满处理流程

## 场景描述

当 PVC 存储空间接近耗尽或已经写满时，常见后果包括应用写入失败、数据库异常、日志堆积、Pod 报错或节点磁盘压力升高。处理时需要确认是业务数据自然增长、日志失控，还是存储侧扩容能力受限。

## 触发条件

- 告警提示 PVC 使用率超过阈值，例如 80%、90% 或 95%
- Pod 日志出现 `No space left on device`
- 应用写入失败、数据库事务报错或日志落盘中断

## 诊断步骤

1. 查看 PVC、PV 与挂载关系，确认哪个卷已接近写满。

```bash
kubectl get pvc,pv -A
kubectl describe pvc <pvc_name> -n <namespace>
kubectl get pod -A -o wide
```

2. 识别哪些 Pod 正在使用该 PVC。

```bash
kubectl get pods -n <namespace> -o yaml
kubectl describe pod <pod_name> -n <namespace>
```

3. 进入业务 Pod 或运维容器检查目录占用情况。

```bash
kubectl exec -n <namespace> <pod_name> -- df -h
kubectl exec -n <namespace> <pod_name> -- du -sh /data/* | sort -h
```

4. 检查使用率趋势，确认增长速度与剩余可用时间。

```text
prometheus_query(query='kubelet_volume_stats_used_bytes{namespace="<namespace>",persistentvolumeclaim="<pvc_name>"}')
prometheus_query(query='kubelet_volume_stats_capacity_bytes{namespace="<namespace>",persistentvolumeclaim="<pvc_name>"}')
prometheus_query(query='predict_linear(kubelet_volume_stats_used_bytes{namespace="<namespace>",persistentvolumeclaim="<pvc_name>"}[6h], 3600 * 24)')
```

5. 检查存储类是否支持在线扩容。

```bash
kubectl get storageclass
kubectl describe storageclass <storageclass_name>
```

## 常见根因

- 日志、临时文件或缓存目录持续膨胀
- 数据保留策略失效，历史文件未清理
- 业务流量增长导致存储容量规划不足
- 存储类不支持在线扩容或扩容流程未生效
- 单个异常任务短时间写入大量数据

## 修复方案

1. 短期止血：清理无用日志、缓存或临时文件。

```bash
kubectl exec -n <namespace> <pod_name> -- rm -rf /data/tmp/*
kubectl exec -n <namespace> <pod_name> -- find /data/logs -type f -mtime +7 -delete
```

2. 在线扩容 PVC：如果存储类支持扩容，直接提高容量。

```bash
kubectl patch pvc <pvc_name> -n <namespace> -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'
```

3. 不支持扩容时：迁移数据到新卷或新实例。

```bash
kubectl get pvc <pvc_name> -n <namespace> -o yaml
kubectl create -f <new_pvc_manifest>.yaml
```

4. 从根因修复：调整日志保留、归档策略或应用写入行为。

```bash
kubectl edit configmap <config_name> -n <namespace>
kubectl rollout restart deployment/<deployment_name> -n <namespace>
```

## 验证步骤

1. 确认 PVC 使用率已经下降或容量已经提升。

```bash
kubectl describe pvc <pvc_name> -n <namespace>
kubectl exec -n <namespace> <pod_name> -- df -h
```

2. 确认业务写入恢复正常，不再出现磁盘空间错误。

```bash
kubectl logs <pod_name> -n <namespace> --tail=200
```

3. 复查容量趋势与告警是否恢复正常。

```text
prometheus_query(query='kubelet_volume_stats_used_bytes{namespace="<namespace>",persistentvolumeclaim="<pvc_name>"}')
prometheus_query(query='kubelet_volume_stats_capacity_bytes{namespace="<namespace>",persistentvolumeclaim="<pvc_name>"}')
```
