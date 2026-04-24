---
name: sre-investigate
description: SRE 故障诊断调查。接收 triage 输出，综合日志、指标和事件分析根因，并产出修复建议。
version: 1.0.0
author: AIOps SRE Agent
license: MIT
metadata:
  hermes:
    tags: [sre, investigate, diagnosis, loki, prometheus, kubernetes]
    related_skills: [sre-triage, sre-remediate, sre-postmortem]
---

# SRE Investigate

用于做第二轮深入诊断，目标是定位根因并形成可执行的修复建议。

## 输入

接收 `triage` 的输出，至少应包含：

- 告警分类结果
- 影响范围判断
- 已收集的 K8s 状态证据
- 已收集的指标摘要

如果输入缺少其中某项，先点明缺口，再继续补证据。

## 目标

完成以下事情：

1. 日志分析
2. 指标趋势分析
3. K8s 事件分析
4. 日志 + 指标 + 事件三方关联
5. 输出根因判断与修复建议

## 工作流

### 1. 建立调查假设

先基于 triage 结果形成 1 到 3 个候选假设，例如：

- 发布引入错误配置
- 资源不足导致 OOM / Pending / 节点压力
- 下游依赖超时或连接失败
- 探针失败导致反复重启

后续的日志和指标查询要围绕这些假设验证，而不是盲搜。

### 2. 日志分析

使用 `loki_query` 查询相关日志。

优先策略：

- 围绕 namespace、app label、pod 名过滤
- 聚焦错误、超时、连接失败、OOM、panic、异常堆栈
- 如果 triage 已发现重启或异常 Pod，优先查这些 Pod 的日志

示例方向：

```text
loki_query(query="{namespace=\"<namespace>\"} |= \"error\"")
loki_query(query="{namespace=\"<namespace>\", pod=~\"<pod-pattern>\"} |= \"timeout\"")
```

### 3. 指标分析

使用 `prometheus_query` 查询指标趋势，重点关注：

- 错误率是否在告警前后突然升高
- 延迟是否持续抬升
- CPU / 内存 / 磁盘 / 网络是否达到瓶颈
- 重启次数、容器存活、副本可用数是否异常

至少对比：

- 当前值
- 告警前后的变化趋势
- 是否存在与日志时间点相吻合的拐点

### 4. 事件分析

使用 `k8s_read` 查询事件与资源详情。

优先命令：

```text
k8s_read(command="kubectl get events -n <namespace> --sort-by=.lastTimestamp")
k8s_read(command="kubectl describe pod <pod> -n <namespace>")
k8s_read(command="kubectl describe deploy <deploy> -n <namespace>")
```

重点识别：

- FailedScheduling
- BackOff / CrashLoopBackOff
- OOMKilled
- Readiness / Liveness probe 失败
- 镜像拉取失败
- 权限或配置挂载失败

### 5. 关联分析

把三类证据串起来，明确回答：

- 最可能的根因是什么
- 哪些证据支持该根因
- 哪些备选假设已被排除
- 是否存在次生影响或连锁故障

如果无法形成高置信结论，必须明确说明“不确定点”和继续调查方向，不能伪造确定性。

## 输出格式

### 调查结论

- `root_cause_summary`
- `confidence`: high / medium / low
- `scope`: 单组件 / 多组件 / 平台级

### 关键证据

- `logs_findings`
- `metrics_findings`
- `events_findings`

### 已排除项

- 列出已验证但不成立的假设

### 修复建议

- 给出 1 到 3 条最可执行的修复建议
- 每条建议说明风险与预期影响
- 明确下一步是否进入 `remediate`

## 约束

- 诊断阶段不要直接执行写操作
- 不要只给“可能是网络问题”这种空泛结论
- 结论必须引用日志、指标、事件三类证据中的至少两类，除非场景明确不适用

