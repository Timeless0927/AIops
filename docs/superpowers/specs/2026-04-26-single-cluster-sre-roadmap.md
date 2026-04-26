# Single-Cluster SRE Agent Roadmap

## Goal

围绕单个 Kubernetes 集群，构建一个面向告警闭环的 SRE 运维机器人。当前阶段优先解决告警处理、上下文绑定、调查分析和安全执行；后续阶段再扩展到趋势分析、预测和主动运维。

本路线图明确坚持两个原则：

- 不做大而全的平台型 SRE 框架
- 先把单集群场景做到稳定、可值班、可复用

## Product Positioning

该项目的目标不是成为通用 AI agent 平台，而是成为一个针对单集群、告警驱动、Feishu 会话协同、Kubernetes 运维闭环的 SRE 机器人。

当前的核心使用路径应始终围绕以下链路展开：

1. Alertmanager 发送告警
2. 系统创建或复用 incident
3. 状态消息进入 Feishu 主群 / thread
4. 运维人员在 thread 中持续追问和协作
5. 机器人完成调查、建议、审批、执行或验证
6. incident 最终进入 resolved / closed

## Why This Roadmap

当前项目已经具备以下基础：

- Alertmanager webhook 入站
- incident store / timeline / dedup / resolved / reopen
- Feishu 主群与 thread 绑定
- incident-aware 上下文回注
- approval / audit / message delivery / system mode
- Kubernetes 工具与命令级审批边界

因此下一阶段最合理的方向不是抽象成更大的平台，而是继续沿闭环运维机器人演进，并在阶段 2 重点增强可观测性分析能力。

## Phase 1: Closed-Loop Operations

### Goal

把当前机器人打磨成“单集群内稳定值班可用”的版本。

### Scope

- 稳定 `Alertmanager -> incident -> Feishu thread -> resolved` 链路
- 稳定 Kubernetes 内部署形态
- 使用 `ServiceAccount + RBAC + kubectl` 完成集群访问
- 确保 incident / approval / audit / message delivery / system mode 可恢复
- 补齐运行文档、告警验证流程和回归清单

### Key Deliverables

- K8S 内可部署的运行时镜像与 manifests
- 稳定的 Feishu thread 回复与 incident 上下文绑定
- 有限但清晰的 K8S 操作能力边界
- 一组标准化回归用例

### Exit Criteria

- 真实告警能够稳定创建 incident 并绑定到 Feishu thread
- 同一 thread 的追问能持续命中同一 incident
- resolved 事件能够闭环到同一 incident
- K8S 操作具备审批、审计和权限边界
- Pod 重启后本地状态仍能恢复

## Phase 2: Observability Analysis

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

为阶段 3 做准备，但暂不直接做预测模型。优先记录：

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
- 后续预测功能所需的基础数据已开始沉淀

## Phase 3: Predictive and Proactive Operations

### Goal

从“被动响应告警”演进到“主动发现风险并提出预防性建议”。

### Scope

阶段 3 只做三类能力，不做泛化 AI 平台：

#### 1. Proactive巡检

- 高重启 Pod
- 长期 Pending / CrashLoopBackOff 资源
- 节点风险状态
- 接近阈值的资源使用情况

#### 2. Trend-Based Early Warning

- 错误率 / 延迟持续恶化
- CPU / 内存 / 磁盘增长异常
- 同类 incident 高频重复
- 告警频率短时间显著上升

#### 3. Preventive Recommendations

- 预测风险窗口
- 建议扩容、限流、重启、配置核查等动作
- 在必要时发起审批建议，但默认不直接自动执行

### Key Deliverables

- 主动巡检任务与风险摘要
- 趋势型预警规则或轻量模型
- 预防性处置建议输出

### Exit Criteria

- 即使未收到 Alertmanager 告警，系统也能识别部分风险
- 主动建议具有较低噪声和可解释性
- 预测与主动建议建立在真实历史 incident 数据之上

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

## Near-Term Priorities

建议未来一段时间按以下顺序推进：

1. 完成 Kubernetes 内部署稳定化
2. 补 incident 级可观测性证据采集
3. 引入结构化分析结果模型
4. 加入相似 case 与重复趋势识别
5. 最后再进入主动巡检与预测预研

## Success Metric

该路线图最终衡量标准不是“功能数量”，而是：

- 机器人能否稳定参与真实值班
- 告警 thread 内能否持续保持上下文和调查质量
- 建议是否基于证据且具备可解释性
- 是否为后续主动运维积累了真实可复用的数据资产
