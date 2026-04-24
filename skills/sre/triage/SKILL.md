---
name: sre-triage
description: SRE 告警分类与快速初筛。接收告警信息，判断严重级别、影响范围，并决定进入 investigate 还是直接 remediate。
version: 1.0.0
author: AIOps SRE Agent
license: MIT
metadata:
  hermes:
    tags: [sre, triage, alert, kubernetes, prometheus]
    related_skills: [sre-investigate, sre-remediate, sre-postmortem]
---

# SRE Triage

用于对告警做第一轮分类、影响评估和分流。

## 输入

优先从用户消息、告警 webhook 或上游流程中提取以下字段：

- `alertname`
- `severity`
- `namespace`
- `description`

如果字段缺失，先明确标注“缺失字段”，再基于已有信息继续做最小可行判断，不要因为字段不全而停止。

## 目标

完成以下事情：

1. 判断告警严重级别：`critical` / `warning` / `info`
2. 评估影响范围：受影响服务、Pod、Deployment、是否用户可感知
3. 获取一轮最基本的 K8s 状态与关键指标证据
4. 输出明确建议：进入 `investigate`，或在证据充分时直接进入 `remediate`

## 工作流

### 1. 规范化告警信息

先整理成统一摘要：

- 告警名称
- 原始严重级别
- 命名空间
- 描述
- 已知受影响对象

如果原始 `severity` 不可信，用以下原则重判：

- `critical`：服务不可用、大面积错误、核心链路中断、用户明显受影响
- `warning`：性能下降、错误率升高、冗余下降、短期内可能恶化
- `info`：提示性事件、容量趋势、状态变化但尚未构成实际故障

### 2. 评估影响范围

至少回答以下问题：

- 哪个 namespace 受影响
- 哪些服务、Deployment、StatefulSet、Pod 可能相关
- 是否是单实例问题还是多副本问题
- 是否影响入口流量、核心 API、数据库、消息队列等关键组件
- 是否可能造成用户感知

判断依据不要空想，优先用工具取证。

### 3. 调用 K8s 状态查询

优先使用 `k8s_read` 获取相关资源状态。

推荐查询模式：

```text
k8s_read(command="kubectl get pods -n <namespace> -o wide")
k8s_read(command="kubectl get deploy -n <namespace>")
k8s_read(command="kubectl describe deploy <name> -n <namespace>")
k8s_read(command="kubectl top pods -n <namespace>")
```

如果告警描述中明确出现服务名、Deployment 名、Pod 名，优先围绕该对象查询。

### 4. 调用 Prometheus 指标查询

使用 `prometheus_query` 获取与告警直接相关的关键指标。

优先查询：

- 可用性或错误率
- 延迟
- CPU / 内存
- 重启趋势
- 请求量或饱和度

如果用户没有提供现成 PromQL，先根据告警语义构造最小查询，不要停在“建议查询什么”这种空泛层面。

### 5. 做分流判断

满足以下任一条件，优先进入 `investigate`：

- 根因仍不明确
- 指标、状态、现象之间存在矛盾
- 涉及多组件联动
- 需要日志或事件进一步确认

只有在以下条件同时满足时，才建议直接进入 `remediate`：

- 根因高度明确
- 修复动作低风险且已有成熟 runbook
- 有清晰的回滚或验证路径

## 输出格式

输出必须包含以下部分：

### 告警分类结果

- `severity`: 重判后的等级
- `confidence`: high / medium / low
- `summary`: 一句话总结当前告警性质

### 影响评估

- `affected_namespace`
- `affected_services`
- `user_impact`
- `blast_radius`

### 已收集证据

- K8s 状态关键发现
- Prometheus 指标关键发现
- 尚未确认的信息

### 下一步建议

- `next_skill`: `investigate` 或 `remediate`
- `reason`: 为什么这样分流
- `recommended_focus`: 下一步优先检查什么

## 约束

- 不要直接执行写操作或高危命令
- 不要在证据不足时给出武断根因
- 不要只复述告警文本，必须补充工具取证结果

