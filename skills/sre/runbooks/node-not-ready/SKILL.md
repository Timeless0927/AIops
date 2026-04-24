# Node NotReady 处理流程

## 场景描述

当节点状态变为 `NotReady` 时，说明控制平面无法确认该节点处于健康可调度状态。这通常会影响节点上 Pod 的运行稳定性，并可能触发调度失败、驱逐或服务容量下降。

## 触发条件

- 监控告警提示 Node `Ready` 条件异常
- `kubectl get nodes` 显示节点状态为 `NotReady`
- 节点上的 Pod 出现驱逐、不可调度或探针失败增多

## 诊断步骤

1. 查看节点状态、条件和最近事件，确认是资源、网络还是 kubelet 健康问题。

```bash
kubectl get nodes -o wide
kubectl describe node <node_name>
kubectl get events -A --field-selector involvedObject.kind=Node --sort-by=.lastTimestamp
```

2. 检查 kubelet 服务状态与日志，确认节点代理是否异常退出或无法连接 APIServer。

```bash
kubectl debug node/<node_name> -it --image=busybox -- chroot /host systemctl status kubelet --no-pager
kubectl debug node/<node_name> -it --image=busybox -- chroot /host journalctl -u kubelet -n 200 --no-pager
kubectl get --raw /api/v1/nodes/<node_name>/proxy/configz
```

3. 检查系统资源是否耗尽，包括磁盘、内存、inode 与 CPU 压力。

```bash
kubectl debug node/<node_name> -it --image=busybox -- chroot /host df -h
kubectl debug node/<node_name> -it --image=busybox -- chroot /host df -i
kubectl debug node/<node_name> -it --image=busybox -- chroot /host free -m
kubectl debug node/<node_name> -it --image=busybox -- chroot /host top -b -n 1 | head -n 20
kubectl top node <node_name>
```

```text
prometheus_query(query='node_memory_MemAvailable_bytes{instance=~"<node_name>.*"}')
prometheus_query(query='node_filesystem_avail_bytes{instance=~"<node_name>.*",mountpoint="/"}')
prometheus_query(query='rate(node_cpu_seconds_total{instance=~"<node_name>.*",mode!="idle"}[5m])')
```

4. 检查网络连通性与关键组件状态。

```bash
kubectl debug node/<node_name> -it --image=busybox -- chroot /host ip addr
kubectl debug node/<node_name> -it --image=busybox -- chroot /host ip route
kubectl debug node/<node_name> -it --image=busybox -- chroot /host ping -c 3 <api_server_ip>
kubectl get pods -n kube-system -o wide
```

5. 如果节点上仍有关键业务 Pod，先评估是否需要隔离节点，避免影响扩大。

```bash
kubectl cordon <node_name>
kubectl get pods -A -o wide --field-selector spec.nodeName=<node_name>
```

## 常见根因

- `kubelet` 进程异常退出或配置损坏
- 节点磁盘、inode、内存耗尽
- 容器运行时异常，例如 containerd/docker 不可用
- 节点与控制平面网络不通，发生网络分区
- 节点系统时间漂移、证书异常或内核故障

## 修复方案

1. kubelet 异常：重启 kubelet 并复查日志。

```bash
kubectl debug node/<node_name> -it --image=busybox -- chroot /host systemctl restart kubelet
kubectl debug node/<node_name> -it --image=busybox -- chroot /host journalctl -u kubelet -n 100 --no-pager
```

2. 磁盘满或 inode 用尽：清理日志、镜像或临时文件。

```bash
kubectl debug node/<node_name> -it --image=busybox -- chroot /host journalctl --vacuum-time=3d
kubectl debug node/<node_name> -it --image=busybox -- chroot /host crictl image prune
kubectl debug node/<node_name> -it --image=busybox -- chroot /host find /var/log -type f -size +500M
```

3. 内存不足：驱逐异常进程，必要时先隔离节点后重启。

```bash
kubectl cordon <node_name>
kubectl drain <node_name> --ignore-daemonsets --delete-emptydir-data
# 重启节点属于高危操作，需要人工通过 SSH、IPMI 或 BMC 执行，不应由集群内 agent 直接触发。
```

4. 网络问题：修复路由、防火墙或 CNI 组件。

```bash
kubectl get pods -n kube-system
kubectl logs -n kube-system <cni_pod_name>
```

## 验证步骤

1. 确认节点重新进入 `Ready` 状态。

```bash
kubectl get nodes
kubectl describe node <node_name>
```

2. 如果之前执行过隔离，确认可以安全恢复调度。

```bash
kubectl uncordon <node_name>
```

3. 检查节点上的关键 Pod 是否恢复正常。

```bash
kubectl get pods -A -o wide --field-selector spec.nodeName=<node_name>
```

4. 复查资源和网络指标是否恢复到正常范围。

```text
prometheus_query(query='kube_node_status_condition{node="<node_name>",condition="Ready",status="true"}')
prometheus_query(query='node_filesystem_avail_bytes{instance=~"<node_name>.*",mountpoint="/"}')
```
