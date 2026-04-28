# Observability Analysis Status

## Goal

记录 Phase 2: Observability Analysis 当前已经落地的最小能力、明确当前实现边界，并收敛下一阶段最值得补齐的真实 collector。

本状态文档不替代 roadmap，也不重新定义整体方向；它只用于说明当前仓库中 Phase 2 已经实现到什么程度。

## Scope

当前收口仅覆盖单集群告警触发后的 incident 级可观测性分析基础：

- incident 级结构化 evidence 存储
- incident 级结构化 analysis 存储
- Alertmanager webhook 进入 Phase 2 采证链路
- Feishu thread 上下文消费结构化 analysis
- 重复 incident 趋势基础字段

当前不覆盖：

- 多集群分析抽象
- 预测模型或主动巡检

## Current State

### 1. Structured Incident Persistence

当前 `toolsets/incident_store.py` 已新增两类结构化存储：

- `incident_evidence`
- `incident_analysis`

其中：

- `incident_evidence` 用于存储单条证据及其时间窗、来源、摘要和 payload
- `incident_analysis` 用于存储当前 incident 的最新分析视图，而不是把分析结果埋进 timeline 长文本

这意味着当前项目已经不再只依赖 `incident_events timeline` 来承载调查信息。

### 2. Evidence Types Already Seeded

当前 `hooks/alert_webhook.py` 在 `alert_fired` 路径上已经会自动播种以下 evidence：

- `alert_window`
  记录告警进入 firing 的时间窗
- `audit_change`
  记录同 namespace / cluster 最近审计变更线索
- `k8s_events`
  记录 namespace 内最近 Kubernetes events 摘要
- `metrics_window`
  记录 Prometheus 时间窗内的关键指标片段
- `logs_window`
  记录 Loki 时间窗内的最小日志摘要
- `workload_topology`
  记录 namespace 内 Pod / Deployment 状态摘要

当前 `metrics_window` 已包含最小但有代表性的四类信号：

- CPU
- Memory
- Restart
- Ready / Unready

这几类信号已经足够支撑第一版单集群 incident 分析。

同时，当前 topology 与 logs 侧也已经有最小接入：

- Pod / Deployment 文本状态摘要
- 最近日志行摘要

### 3. Analysis Fields Already Used

当前 analysis 已实际使用以下字段，而不是只做 schema 预留：

- `symptoms`
- `likely_scope`
- `suspected_root_causes`
- `supporting_evidence`
- `missing_evidence`
- `next_best_actions`
- `confidence`

当前 `suspected_root_causes` 已经能基于规则化证据命中补充候选原因，例如：

- 近期变更可能引发工作负载异常
- Kubernetes events 显示工作负载异常
- 资源压力可能导致工作负载异常
- 工作负载健康状态异常
- 应用日志显示运行时异常
- 工作负载拓扑状态显示副本或 Pod 异常

当前 `next_best_actions` 也已经能随 evidence 类型动态调整，而不是固定占位动作。

### 4. Confidence Is No Longer Static

当前 analysis 的 `confidence` 已不再固定为常量，而是根据已命中的证据类型做最小归并：

- `audit_change` 命中会提高 confidence
- `k8s_events` 命中会提高 confidence
- 资源压力信号命中会提高 confidence
- workload 健康异常信号命中会提高 confidence

这使得 thread 回复已经具备“结论强弱”表达基础。

### 5. Feishu Thread Consumption

当前 `hooks/voice_context.py` 已优先消费 `incident_analysis`，并在 thread 上下文中明确展示：

- `Top根因`
- `Top下一步`

在没有 analysis 时，仍回退到现有 timeline 摘要，因此 Phase 1 的会话闭环兼容性保持不变。

### 6. Trend Baseline Preparation

当前 `toolsets/sre_metrics.py` 已新增最小趋势基础字段：

- `repeat_incident_count`
- `repeat_incident_rate`

这部分仍然是 Phase 3 的预留基础，而不是预测能力本身。

### 7. Case Profile Persistence

当前 resolved incident 已会自动沉淀 `case profile`。

其中至少包括：

- `incident_signature`
- `symptom_fingerprint`
- `final_scope`
- `final_root_cause`
- `effective_actions`
- `metric_delta_summary`
- `change_clue_summary`
- `resolution_seconds`
- `similar_incident_ids`

这意味着 Phase 2 到 Phase 3 的数据承接层已经开始真实落库，而不是只停留在设计层。

### 8. Similar Case Reuse

当前 case profile 已支持基于 `incident_signature` 查询最近相似 case，并在新的 resolved case profile 中回填 `similar_incident_ids`。

这意味着“技能学习沉淀”已经不只是静态归档，而是开始具备最小复用路径。

## Current Boundaries

当前实现仍然刻意保持以下边界：

- 只围绕单个 incident 做结构化分析
- 只在 Alertmanager firing 路径自动播种 evidence
- 不引入新的平台型 orchestration 或 memory 抽象
- 不做日志总结、相似 case 检索或根因模型推理
- 不改变 Phase 1 的 approval / audit / execution 主路径

这符合当前 roadmap 中“单集群优先、先证据再预测、先结构化数据再做更智能能力”的原则。

## What Is Still Missing

尽管 Phase 2 已有最小闭环，但仍有几块关键能力尚未落地：

### 1. Deeper Workload Topology Evidence

当前 topology 已有最小 evidence，但仍是文本摘要，尚未进一步结构化为：

- Pod phase / restart 排名
- Deployment 副本变化摘要
- Node 归属与漂移线索

### 2. Similar Case Ranking

当前已经有 case profile 与基于 `incident_signature` 的相似 case 回填，但还没有更细的排序或 fingerprint 级检索。

### 3. Confidence Calibration

当前 confidence 只是规则化累加，是可用的第一版，但还不是严格校准过的置信体系。

## Recommended Next Steps

如果继续 Phase 2，下一阶段最值得补的顺序建议如下：

1. 把 workload topology 从文本摘要升级为更结构化的字段
2. 增强相似 case 排序与 fingerprint 复用
3. 校准 confidence 规则
4. 再做趋势复用与主动预警承接

## Summary

当前仓库中的 Phase 2 已经不再是 roadmap 占位，而是具备了一个可工作的最小分析闭环：

- 告警进入 incident 后会自动播种多源 evidence
- evidence 会归并成结构化 analysis
- analysis 会被 Feishu thread 上下文直接消费
- analysis 已具备初步的根因候选、下一步动作和结论置信度
- resolved incident 已会自动沉淀 case profile，并开始回填历史相似 case
- Pod / Deployment / Node / Logs / Metrics / Events / Change 这几类关键证据都已经进入最小闭环

这说明项目已经从“只有告警闭环和执行边界”进入“开始具备 incident 级调查分析能力”的阶段。
