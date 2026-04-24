# Pod CrashLoopBackOff 处理流程

## 场景描述

当 Pod 持续进入 `CrashLoopBackOff` 状态时，表示容器启动后很快退出，Kubernetes 正在反复重启容器。这类问题通常会导致服务实例不可用、流量抖动或发布失败，需要先确认故障范围，再定位具体退出原因。

## 触发条件

- 监控或告警出现 `CrashLoopBackOff`、`Back-off restarting failed container`
- `kubectl get pods -n <namespace>` 显示 Pod 状态反复为 `CrashLoopBackOff`
- 服务可用实例数下降，Deployment/StatefulSet 无法达到期望副本数

## 诊断步骤

1. 确认异常 Pod 的基础状态与最近事件。

```bash
kubectl get pods -n <namespace> -o wide
kubectl describe pod <pod_name> -n <namespace>
kubectl get events -n <namespace> --sort-by=.lastTimestamp
```

2. 查看上一次退出容器的日志，优先抓取真正导致退出的报错。

```bash
kubectl logs <pod_name> -n <namespace> --previous
kubectl logs <pod_name> -n <namespace> -c <container_name> --previous
```

3. 检查资源限制与实际资源压力，确认是否存在 OOM 或 CPU 节流。

```bash
kubectl describe pod <pod_name> -n <namespace>
kubectl top pod <pod_name> -n <namespace> --containers
kubectl get deployment <deployment_name> -n <namespace> -o yaml
```

```text
prometheus_query(query='container_memory_working_set_bytes{namespace="<namespace>",pod="<pod_name>"}')
prometheus_query(query='rate(container_cpu_usage_seconds_total{namespace="<namespace>",pod="<pod_name>"}[5m])')
```

4. 检查探针配置是否错误，确认应用是否在启动阶段就被探针杀掉。

```bash
kubectl get pod <pod_name> -n <namespace> -o yaml
kubectl describe pod <pod_name> -n <namespace>
```

重点检查：

- `livenessProbe` 是否过早执行
- `readinessProbe` 路径、端口、协议是否正确
- `startupProbe` 是否缺失导致慢启动应用被误判

5. 检查依赖配置与镜像可用性。

```bash
kubectl get configmap,secret -n <namespace>
kubectl describe pod <pod_name> -n <namespace>
kubectl get deploy <deployment_name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[*].image}'
```

## 常见根因

- 容器内存不足，触发 `OOMKilled`
- 应用配置错误，启动参数、环境变量、配置文件不正确
- 下游依赖不可用，例如数据库、Redis、消息队列连接失败
- 镜像版本错误、镜像拉取异常或镜像内启动命令有问题
- 探针配置过严，应用尚未完成启动就被重启

## 修复方案

1. OOM 场景：提高资源限制或降低应用启动峰值。

```bash
kubectl set resources deployment/<deployment_name> -n <namespace> \
  --limits=memory=1Gi,cpu=1 \
  --requests=memory=512Mi,cpu=200m
```

2. 配置错误场景：修复 ConfigMap/Secret 后重启工作负载。

```bash
kubectl edit configmap <configmap_name> -n <namespace>
kubectl rollout restart deployment/<deployment_name> -n <namespace>
```

3. 依赖服务不可用场景：先恢复依赖，再观察业务 Pod。

```bash
kubectl get svc,endpoints -n <namespace>
kubectl rollout status deployment/<dependency_name> -n <namespace>
```

4. 镜像问题场景：回滚到稳定版本或修复镜像标签。

```bash
kubectl rollout undo deployment/<deployment_name> -n <namespace>
kubectl set image deployment/<deployment_name> <container_name>=<image>:<stable_tag> -n <namespace>
```

5. 探针问题场景：放宽探针阈值或增加 `startupProbe`。

```bash
kubectl edit deployment <deployment_name> -n <namespace>
```

## 验证步骤

1. 确认 Pod 已恢复到 `Running` 且重启次数不再持续增长。

```bash
kubectl get pods -n <namespace> -w
kubectl describe pod <pod_name> -n <namespace>
```

2. 确认 Deployment 副本已全部就绪。

```bash
kubectl rollout status deployment/<deployment_name> -n <namespace>
```

3. 复查资源与错误指标是否回落。

```text
prometheus_query(query='kube_pod_container_status_restarts_total{namespace="<namespace>",pod="<pod_name>"}')
prometheus_query(query='sum(rate(container_cpu_usage_seconds_total{namespace="<namespace>"}[5m])) by (pod)')
```
