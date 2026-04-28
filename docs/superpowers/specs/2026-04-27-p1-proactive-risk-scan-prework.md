# P1 Proactive Risk Scan Prework

## Goal

在不重做 P0 闭环主线与既有可观测性能力的前提下，为后续主动运维方向保留第一批可工作的 P1 预研能力：基于单集群当前实时状态与已沉淀的 incident / case profile 数据，主动发现低噪声风险并生成可解释的风险摘要。

本设计的目标不是“预测模型上线”，而是先把主动风险扫描这条链路跑通：

1. 不依赖 Alertmanager 告警触发。
2. 能围绕真实集群状态输出结构化风险项。
3. 能引用历史 incident / case profile 提供预防性建议。
4. 能通过回归测试稳定验证，不依赖线上值班场景。

## Scope

本次 prework 只覆盖 P1 预研的最小闭环，不扩展到整套预测系统，也不提升为当前阶段的主线承诺。

### In Scope

- 单集群主动风险扫描工具。
- 第一批低噪声风险类型：
  - 高重启 Pod
  - Unready workload
  - Node 风险状态
- 基于已有 `incident_case_profiles` 的历史 case 提示。
- 基于已有 `sre_metrics.compute_metrics()` 的重复 incident 基线提示。
- 面向人类值班者的结构化风险摘要与中文摘要输出。
- 对应的单元测试与插件注册回归。

### Out of Scope

- 自动调度、Cron、周期化巡检任务编排。
- 复杂时间序列预测模型。
- 多集群支持。
- 自动 remediation 或自动审批执行。
- 新的平台型 orchestration / memory 抽象。
- 将现有 evidence / analysis 流程泛化重写。

## Relationship To P0

这份文档服务于 P1 预研，不改变当前 roadmap 中的主次关系：

- P0 仍然是值班闭环主线。
- P1 只允许以手动、只读、低噪声、可回放的形态存在。
- 如果 P0 的 incident / approval / execution / verification 主链路还不稳定，P1 不应抢占交付节奏。

## Constraints

- 坚持单集群优先，不为多集群预埋复杂抽象。
- 优先复用现有 `k8s_read`、`prometheus_query`、`incident_store`、`sre_metrics`。
- 风险结论必须可解释，不能只给分数不给证据。
- 第一批能力应支持离线测试：用 stubbed `k8s_read` / `prometheus_query` / `incident_store` 即可验证。
- 如果实时数据不足，应优雅降级为“无风险”或“缺少证据”，而不是制造高噪声结论。

## Recommended Approach

推荐新增一个独立工具模块，例如 `toolsets/proactive_risk_scan.py`，作为 P1 预研的最小承载单元。

该模块负责三类职责：

1. 采集主动巡检所需的最小实时信号。
2. 基于确定性规则生成结构化风险项。
3. 结合历史 case profile 与 repeat incident baseline 生成预防性建议与摘要。

推荐将第一批能力暴露为可手动调用的只读工具，例如 `sre_proactive_risk_scan`，而不是一开始就做自动定时任务。这样做有三个原因：

- 更符合当前项目节奏，先把逻辑做稳。
- 更容易通过测试和人工回放校验噪声水平。
- 后续如果需要调度，只需复用这个工具，不需要重写判定逻辑。

## Why A Manual Tool First

P1 预研的首要风险不是“调度还不够自动化”，而是“规则质量不稳、解释性不足、误报难以控制”。

因此第一批实现先做一个手动可调用的工具，收益更明确：

- 能快速验证规则是否合理。
- 能在 Feishu、终端、回放测试中复用同一输出。
- 能避免调度、去重、通知节流等外围问题干扰核心判定逻辑。

自动调度可以作为下一批增强项，在现有工具稳定后再追加。

## Architecture

第一批主动风险扫描的逻辑分为四层：

### 1. Live Signal Collection

从现有只读工具采集当前集群信号：

- `k8s_read("kubectl get pods -A")`
- `k8s_read("kubectl get deploy -A")`
- `k8s_read("kubectl get nodes")`
- 可选的 `prometheus_query(...)` 用于补充重启或 Unready 的时间窗趋势

第一批实现不要求采集大量 Prometheus 指标。只要能支撑风险解释即可。

### 2. Rule-Based Risk Detection

基于低复杂度规则生成风险项：

- 若 Pod 重启次数超过阈值，则生成 `high_restart_pod` 风险。
- 若 Deployment 的 ready / available 副本明显异常，则生成 `unready_workload` 风险。
- 若 Node 为 `NotReady` 或出现显著风险状态，则生成 `node_risk` 风险。

规则应尽量直接读取 `kubectl get` 文本输出中的稳定列，不引入复杂解析依赖。

### 3. Historical Context Enrichment

风险项生成后，再引入历史上下文而不是先做历史匹配驱动判定。

第一批历史上下文来源：

- `incident_case_profiles`
- `sre_metrics.compute_metrics(days=N)` 返回的 `repeat_incident_count` / `repeat_incident_rate`

历史上下文的作用不是决定是否告警，而是：

- 为风险项补充“近期同类 case 是否频繁出现”
- 为风险项补充“过去有效动作是什么”
- 为最终摘要补充“当前集群处于怎样的重复事件基线”

### 4. Human-Readable Summary

最终输出两类结果：

- 结构化 `risks` 列表，供后续 Feishu、自动任务或其他流程消费。
- 简洁中文 `summary`，供人类值班者快速浏览。

## Risk Model

第一批风险项推荐使用统一字典结构，而不是过早引入复杂类型层。

每条风险至少包含：

- `risk_type`
- `severity`
- `scope`
- `resource_ref`
- `summary`
- `supporting_evidence`
- `historical_context`
- `recommended_actions`
- `confidence`

推荐示例：

```json
{
  "risk_type": "high_restart_pod",
  "severity": "warning",
  "scope": "workload",
  "resource_ref": "default/pod/api-7d8c9f6c6f-x2m5q",
  "summary": "Pod api-7d8c9f6c6f-x2m5q restart count is high (12)",
  "supporting_evidence": [
    "kubectl get pods -A shows RESTARTS=12",
    "namespace default had 2 recent resolved workload cases"
  ],
  "historical_context": {
    "recent_case_count": 2,
    "common_effective_actions": [
      "检查 Pod CPU/内存指标与资源配置",
      "检查相关 Pod 最近错误日志与超时信息"
    ]
  },
  "recommended_actions": [
    "检查该 Pod 最近 15 分钟日志",
    "核对该工作负载资源配置与最近变更"
  ],
  "confidence": 0.72
}
```

## First-Batch Detection Rules

第一批规则故意保持简单、透明、可测试。

### High Restart Pod

输入：`kubectl get pods -A`

最小规则：

- 仅关注 `Running` 但 `RESTARTS` 较高的 Pod。
- 默认阈值可先定为 `>= 5`。
- 如果 Pod 同时处于 `CrashLoopBackOff` 或 `Error`，则可以提升严重度。

推荐输出：

- `severity=warning` 或 `critical`
- `scope=workload`
- 建议动作偏向日志、事件、资源核查。

### Unready Workload

输入：`kubectl get deploy -A`

最小规则：

- `AVAILABLE < DESIRED` 时判定为存在风险。
- 若差值较大或副本为 0，可提升严重度。

推荐输出：

- `severity=warning` 或 `critical`
- `scope=workload`
- 建议动作偏向副本、探针、事件和变更核查。

### Node Risk

输入：`kubectl get nodes`

最小规则：

- `STATUS != Ready` 时直接判定风险。
- 若文本状态包含 `SchedulingDisabled`、`NotReady` 等信号，可提升严重度。

推荐输出：

- `severity=critical`
- `scope=node`
- 建议动作偏向节点状态、资源压力、受影响工作负载分布核查。

## Historical Case Reuse

第一批不尝试做“语义相似 case 检索”，只做同 namespace / 同 scope 的最近 case 复用。

原因：

- 当前 `incident_case_profiles` 已真实落库，可直接复用。
- 现有 `incident_signature` 与 `symptom_fingerprint` 尚不适合直接承担复杂相似度排序。
- 同 namespace / 同 scope 的最近 resolved case 已足够支撑第一批预防性建议。

推荐做法：

- 新增 `incident_store` 只读查询接口，用于按 `namespace`、`final_scope` 查询最近 case profiles。
- 对结果做最小聚合：
  - 最近 case 数量
  - 高频 `final_root_cause`
  - 高频 `effective_actions`

这些信息只用于 enrich 风险项与摘要，不直接决定风险是否成立。

## Repeat Baseline Reuse

第一批直接复用 `sre_metrics.compute_metrics(days=N)` 产出的：

- `repeat_incident_count`
- `repeat_incident_rate`

这部分信号不宜绑定到单条资源上，而应作为本次扫描摘要的全局背景，例如：

- 最近 7 天重复 incident 比例偏高，建议优先关注反复出现的 workload 风险。

这样既复用了现有基础数据，又避免把全局统计误用成单资源结论。

## Summary Format

最终摘要应同时满足“人能快速看懂”和“机器可继续消费”两点。

推荐返回结构：

```json
{
  "ok": true,
  "scanned_at": 1710000000.0,
  "cluster_risk_baseline": {
    "repeat_incident_count": 3,
    "repeat_incident_rate": 0.25
  },
  "risks": [...],
  "summary": "主动巡检发现 3 项风险：2 项 workload 风险、1 项 node 风险。最近 7 天重复 incident 比例为 25.0%，建议优先核查 default 命名空间内高重启与副本异常工作负载。"
}
```

当没有发现风险时，应返回明确的低噪声结果：

```json
{
  "ok": true,
  "risks": [],
  "summary": "主动巡检未发现高重启 Pod、Unready workload 或 Node Ready 风险。"
}
```

## Integration Surface

第一批集成面应保持最小：

- 新增工具模块并注册到 SRE 插件。
- 插件 manifest 中暴露 `sre_proactive_risk_scan`。
- 增加工具级测试，确保插件可发现该工具。

第一批不要求：

- 修改 `alert_webhook`。
- 修改 `voice_context`。
- 将风险扫描结果自动落入 incident timeline。

这些都可以作为下一批集成增强。

## Not A Current Product Promise

这份预研文档明确不是以下承诺：

- 不是当前阶段默认开启的主动巡检产品。
- 不是会自动推送到值班通道的新告警源。
- 不是会自动执行修复动作的自治系统。
- 不是多集群风险雷达的起点。

它唯一要证明的是：主动风险扫描是否能以低噪声、可解释、可回放的方式成立。

## Error Handling

主动风险扫描应采用“局部失败、整体降级”的策略。

- 若 `kubectl get pods -A` 失败，Pod 风险扫描可返回空结果并在摘要中注明证据不足。
- 若 `compute_metrics()` 失败，历史 repeat baseline 可降级为空。
- 若 case profile 查询失败，仍允许返回实时风险，只是不补充历史建议。

第一批不应因为单个数据源失败而整次扫描报错终止。

## Testing Strategy

第一批测试重点是规则稳定性，而不是环境联通性。

至少覆盖：

- 高重启 Pod 命中。
- Deployment 副本异常命中。
- Node NotReady 命中。
- 最近 case profile 能补充推荐动作。
- repeat baseline 能进入全局摘要。
- 所有输入都健康时返回低噪声空风险结果。
- 插件 manifest / registry 能发现新工具。

测试应以 stubbed 依赖为主：

- fake `k8s_read`
- fake `prometheus_query`
- fake `incident_store`
- fake `compute_metrics`

## Exit Criteria

本 prework 完成的定义是：

- 系统具备一个可调用的主动风险扫描工具。
- 工具能围绕高重启 Pod、Unready workload、Node 风险生成结构化结果。
- 风险摘要能引用已有 case profile / repeat baseline，而不是纯实时快照。
- 输出低噪声、可解释、可通过回归测试稳定验证。

满足这些条件后，能力仍然停留在 P1 预研层，不自动升级为当前主线阶段。

## Follow-Up After This Prework

在这批 prework 完成后，再考虑下一批增强：

- 自动定时巡检与通知节流。
- 更细的趋势型 early warning。
- 更好的 case ranking 与 fingerprint 复用。
- 将主动风险与 Feishu thread / incident 工作流做更紧密集成。

在这些前提完成之前，不建议直接跳到复杂预测模型。
