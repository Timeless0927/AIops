# 内存过高处理流程

## 场景描述

当容器或节点的内存使用率持续升高时，可能导致应用响应变慢、频繁 GC、`OOMKilled`，甚至引发节点级资源争抢。处理这类问题需要先识别热点对象，再区分是短时流量峰值还是持续性内存泄漏。

## 触发条件

- 告警提示容器、Pod 或节点内存使用率超过阈值
- 应用日志中出现 OOM、GC 频繁、分配失败等信号
- `kubectl top` 显示某些 Pod 或节点内存持续偏高

## 诊断步骤

1. 快速定位内存消耗最高的 Pod 和节点。

```bash
kubectl top pods -A --sort-by=memory
kubectl top nodes
kubectl get pods -A -o wide
```

2. 查看工作负载资源配置，确认 request/limit 是否合理。

```bash
kubectl get deployment <deployment_name> -n <namespace> -o yaml
kubectl describe pod <pod_name> -n <namespace>
```

3. 检查指标趋势，确认是瞬时峰值还是长期增长。

```text
prometheus_query(query='container_memory_working_set_bytes{namespace="<namespace>",pod="<pod_name>"}')
prometheus_query(query='max_over_time(container_memory_working_set_bytes{namespace="<namespace>",pod="<pod_name>"}[1h])')
prometheus_query(query='node_memory_MemAvailable_bytes{instance=~"<node_name>.*"}')
```

4. 结合日志检查是否存在内存泄漏、缓存膨胀或请求堆积。

```bash
kubectl logs <pod_name> -n <namespace> --tail=200
kubectl logs <pod_name> -n <namespace> -c <container_name> --tail=200
```

```text
loki_query(query='{namespace="<namespace>",pod="<pod_name>"} |= "OOM"')
loki_query(query='{namespace="<namespace>",pod="<pod_name>"} |= "OutOfMemory"')
```

5. 评估是否受流量增长影响，确认是否需要水平扩容分摊压力。

```text
prometheus_query(query='sum(rate(http_requests_total{namespace="<namespace>",pod=~"<pod_prefix>.*"}[5m]))')
```

## 常见根因

- 应用内存泄漏，长时间运行后内存只增不减
- 缓存、队列或批处理任务堆积
- 流量突增导致单实例负载超出预期
- 资源 limit 配置过小，正常业务峰值下也会触发 OOM
- 节点超卖严重，多个 Pod 同时争抢内存

## 修复方案

1. 短期止血：重启异常 Pod，快速释放内存。

```bash
kubectl delete pod <pod_name> -n <namespace>
```

2. 水平扩容：增加副本数分摊流量和内存压力。

```bash
kubectl scale deployment/<deployment_name> -n <namespace> --replicas=<count>
```

3. 调整资源限制：提高 `requests/limits`，避免误伤正常流量峰值。

```bash
kubectl set resources deployment/<deployment_name> -n <namespace> \
  --requests=memory=1Gi,cpu=500m \
  --limits=memory=2Gi,cpu=1
```

4. 泄漏场景：回滚到稳定版本或推动研发修复内存泄漏。

```bash
kubectl rollout undo deployment/<deployment_name> -n <namespace>
kubectl rollout history deployment/<deployment_name> -n <namespace>
```

5. 节点级压力：迁移工作负载或扩容节点池。

```bash
kubectl cordon <node_name>
kubectl drain <node_name> --ignore-daemonsets --delete-emptydir-data
```

## 验证步骤

1. 复查 Pod 与节点内存指标是否明显回落。

```bash
kubectl top pods -A --sort-by=memory
kubectl top nodes
```

```text
prometheus_query(query='container_memory_working_set_bytes{namespace="<namespace>",pod="<pod_name>"}')
prometheus_query(query='node_memory_MemAvailable_bytes{instance=~"<node_name>.*"}')
```

2. 确认应用未再出现 OOM 或重启增长。

```bash
kubectl describe pod <pod_name> -n <namespace>
```

3. 观察一段时间内趋势是否稳定，避免只解决瞬时症状。

```text
prometheus_query(query='increase(kube_pod_container_status_restarts_total{namespace="<namespace>",pod="<pod_name>"}[30m])')
```
