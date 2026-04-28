# Single-Cluster SRE Agent Roadmap

## Goal

围绕单个 Kubernetes 集群，构建一个面向真实值班场景的自治运维助手。

当前阶段的产品主线不是“主动发现一切问题”，而是先把一条稳定、可信、可审计的闭环打穿：

`Alertmanager -> incident -> evidence -> analysis -> suggestion -> approval -> execution -> verification -> resolved`

在这条闭环稳定之前，主动运维不应作为主线产品承诺；它只应作为预留方向和轻量预研存在。

本路线图明确坚持五个原则：

- 不做大而全的平台型 SRE 框架
- 单集群优先，不为多集群提前复杂化
- 先闭环，再扩展
- 先证据，再预测
- 先建议，再自动执行

## Product Positioning

该项目的目标不是成为通用 AI agent 平台，而是成为一个针对单集群、告警驱动、Feishu 会话协同、Kubernetes 运维闭环的 SRE 机器人。

用户真正购买的不是“AI 会聊天”，而是：

- 告警来了有人接
- 调查过程有证据
- 动作前有人机协同和审批
- 动作后有验证、审计和沉淀

当前的核心使用路径应始终围绕以下链路展开：

1. Alertmanager 发送告警
2. 系统创建或复用 incident
3. 状态消息进入 Feishu 主群 / thread
4. 运维人员在 thread 中持续追问和协作
5. 机器人完成调查、建议、审批、执行或验证
6. incident 最终进入 resolved / closed
7. resolved incident 沉淀为可复用 case profile / runbook 输入

## Strategy Reset

从当前阶段开始，项目范围收敛为两层：

### P0: 必做闭环主线

P0 是当前唯一主战场。

目标是把机器人打磨成“单集群内稳定值班可用”的版本，让它能真实接住 incident 并推进到结束，而不是只会答复或给建议。

### P1: 预留但不承诺的主动运维

P1 不是取消，而是降级处理。

主动运维相关能力可以继续保留数据结构、只读工具和离线验证，但不应成为当前阶段的对外核心承诺，也不应抢占 P0 的交付节奏。

### Not Now

以下方向当前不进入主线：

- 多集群抽象
- 平台型 orchestration / memory 重写
- 复杂预测模型
- 默认开启的定时巡检推送
- 无审批自动 remediation

## What Already Exists

当前项目已经具备一批足够珍贵的基础，不需要重讲一个新故事：

- Alertmanager webhook 入站
- incident store / timeline / dedup / resolved / reopen
- Feishu 主群与 thread 绑定
- incident-aware 上下文回注
- approval / audit / message delivery / system mode
- Kubernetes 工具与命令级审批边界
- incident 级 evidence / analysis 结构化存储
- repeat incident 基线字段与 case profile 沉淀

因此下一阶段最合理的方向不是抽象成更大的平台，而是继续沿闭环运维机器人演进，把已有基础打磨成稳定值班能力。

## P0: Closed-Loop Operations

### Goal

把当前机器人打磨成“单集群内稳定值班可用”的版本。

这里的“可用”不是 demo 可跑，而是值班人员愿意在真实 incident 中持续依赖它。

### Scope

P0 必做范围如下：

- 稳定 `Alertmanager -> incident -> Feishu thread -> resolved` 链路
- 稳定 Kubernetes 内部署形态
- 使用 `ServiceAccount + RBAC + kubectl` 完成集群访问
- 围绕单个 incident 自动采集多源证据并沉淀结构化 analysis
- 在 thread 中输出可解释的调查结论、影响范围和下一步建议
- 为一批边界清晰的低风险动作提供审批、执行、验证和审计闭环
- 确保 incident / approval / audit / message delivery / system mode 可恢复
- 在 incident 关闭后沉淀 case profile，为后续复用提供基础
- 补齐运行文档、告警验证流程和回归清单

### Key Deliverables

- K8S 内可部署的运行时镜像与 manifests
- 稳定的 Feishu thread 回复与 incident 上下文绑定
- incident 级 evidence / analysis 结构化模型
- 有限但清晰的 K8S 操作能力边界
- 审批后执行与结果验证闭环
- 一组标准化回归用例
- resolved incident 的 case profile 沉淀链路

### Exit Criteria

P0 完成应至少满足以下条件：

- 真实告警能够稳定创建 incident 并绑定到 Feishu thread
- 同一 thread 的追问能持续命中同一 incident
- evidence 与 analysis 能稳定支持高质量下一步建议
- 至少一批边界清晰的低风险动作能够审批后稳定执行并验证结果
- resolved 事件能够闭环到同一 incident
- resolved incident 能稳定沉淀为可复用 case profile
- K8S 操作具备审批、审计和权限边界
- Pod 重启后本地状态仍能恢复

### Out of Scope for P0

以下事项明确不属于 P0：

- 周期化主动巡检编排
- 主动风险自动推送
- 趋势预测模型上线
- 自动 remediation 或自动审批执行
- 多集群支持

## Phase 2: Observability Analysis

Status: complete (current-stage MVP closeout)

### Goal

增强机器人对单个 incident 的证据采集、关联分析和趋势理解能力，使其从“会答复”升级到“会分析”。

### Direction

阶段 2 的核心不是更强的自动执行，而是更强的可观测性分析。

机器人需要围绕告警时间窗自动组织更多调查证据，并将其沉淀为结构化 incident 上下文，而不是仅靠单轮对话拼接长文本。

### Workstreams

#### 1. Time-Window Evidence Collection

围绕告警发生时间，自动采集并关联以下证据：

- 相关 Pod / Deployment / Node 状态
- Kubernetes events
- 告警前后关键指标片段
- 相关日志摘要
- 最近变更线索

#### 2. Structured Incident Evidence

在 timeline 之外，增加结构化证据模型，例如：

- `symptoms`
- `likely_scope`
- `suspected_root_causes`
- `supporting_evidence`
- `missing_evidence`
- `next_best_actions`

这样后续 thread 回复将基于 incident 证据对象，而不是纯文本上下文。

#### 3. Multi-Source Correlation

让机器人能够更稳定地判断：

- 问题是单 Pod 级、工作负载级还是节点级
- 更可能是应用异常、资源瓶颈、节点问题还是变更引发
- 当前证据是否足以下结论
- 下一步最值得执行的排查动作是什么

#### 4. Trend Baseline Preparation

为 P1 做准备，但暂不直接做预测模型。优先记录：

- 告警前后指标变化
- 同类 incident 的重复频率
- 最近相似 case
- 资源健康趋势摘要

### Key Deliverables

- incident 级结构化调查证据模型
- 多源证据采集接口
- 更像 SRE 调查报告的 thread 回复能力
- 同类 incident 的重复性和趋势识别基础

### Exit Criteria

- 同一 incident 能自动沉淀结构化调查证据
- 回复能明确说明证据来源、影响范围和结论置信度
- 机器人能给出更高质量的下一步排查建议
- 后续主动运维所需的基础数据已开始沉淀

## P1: Reserved Proactive Operations Track

### Goal

在不打断 P0 主线的前提下，为未来主动风险发现建立低噪声、可解释、可回放验证的基础能力。

P1 不是当前产品中心，而是 P0 之后的第二增长曲线。

### What P1 Is Allowed To Build Now

P1 当前允许的工作应严格收敛在以下范围：

- 保留并扩展 incident / case profile / repeat metrics 等数据承接层
- 提供手动触发的只读 `risk scan` 工具
- 为主动风险识别补离线回放与回归测试
- 校准规则质量、可解释性和噪声水平
- 复用已有 evidence / analysis / case profile，而不是重做一套系统

### What P1 Must Not Become Yet

在 P0 完成之前，P1 不应演变成以下形态：

- 自动调度、Cron、周期化巡检任务编排
- 主动风险默认推送到值班通道
- 复杂时间序列预测模型
- 自动 remediation 或自动审批执行
- 新的平台型 orchestration / memory 抽象
- 为主动运维单独扩张多集群抽象

### Graduation Criteria

只有在以下条件满足后，P1 才可以升级为新的主线阶段：

- P0 的闭环值班能力已稳定
- 已沉淀足够数量的真实 incident 与 case profile
- 手动 `risk scan` 在回放和人工使用中证明噪声可控
- 主动建议能够解释“为什么值得看”和“下一步该做什么”
- 团队确认主动建议不会成为新的告警噪声源

## What To Borrow From OpenSRE

`opensre` 更适合作为未来架构参考，而不是当前实现模板。

适合借鉴的部分：

- agent 能力要可演进，而不是一次性脚本
- workflow 应明确建模，而不是完全自由对话
- 训练 / 评测 / 回放思路值得提前预留接口
- 工具层和 workflow 层应保持边界清晰

当前不适合直接照搬的部分：

- 大平台抽象
- 多 agent 编排优先
- 训练与 benchmark 系统先行
- 为通用性牺牲当前 Feishu + Alertmanager + K8S 闭环

## Engineering Principles

后续所有实现都应遵守以下原则：

- 单集群优先，不为多集群提前复杂化
- 先闭环，再扩展
- 先证据，再预测
- 先建议，再自动执行
- 先结构化数据，再做更智能的能力
- 主动能力必须证明自己不会制造新的值班噪声

## Near-Term Priorities

建议未来一段时间按以下顺序推进：

1. 完成 Kubernetes 内部署稳定化
2. 稳定 incident / approval / audit / recovery 主链路
3. 补 incident 级可观测性证据采集与结构化 analysis 细节
4. 提升 case profile、相似 case 复用和 confidence 校准
5. 验证一批低风险 remediation 的审批、执行和结果确认闭环
6. 只以手动只读工具形态推进主动风险扫描预研

## Success Metric

该路线图最终衡量标准不是“功能数量”，而是：

- 机器人能否稳定参与真实值班
- 告警 thread 内能否持续保持上下文和调查质量
- 建议是否基于证据且具备可解释性
- 审批后的动作是否可控、可验证、可审计
- resolved incident 是否持续沉淀为可复用的数据资产
- P1 预研是否在不制造噪声的前提下，为未来主动运维积累了真实基础
