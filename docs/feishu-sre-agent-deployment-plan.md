# 飞书落地 AIOps SRE Agent 方案

## 1. 目标

本文档描述 AIOps SRE Agent 在飞书场景下的推荐落地方案。核心目标不是单纯让 Agent 能回复消息，而是让它在真实运维场景中具备以下能力：

- 多告警并发时不串上下文
- 主群消息简洁，排障细节下沉到独立 Thread
- 支持审批、审计、交接、恢复和复盘
- 控制 LLM 上下文长度和调用成本
- 为后续从单机部署演进到多实例部署保留路径

推荐方案为：`飞书 Thread 级 Session 隔离 + SQLite 事件库兜底长期记忆`。

但更准确的定义应当是：`Thread 负责交互容器，SQLite 中的 incident state 才是真实会话和事实源`。

## 2. 为什么采用这个方案

飞书群聊天然适合做告警入口，但不适合作为长期状态存储，也不适合承载多事件并行排障的全部细节。如果把所有诊断过程都直接堆在主群，会出现几个明显问题：

- 多个告警混在同一时间线里，容易串上下文
- 历史消息越积越多，LLM 上下文越来越长
- 审批、修复、回滚、交接记录缺少结构化沉淀
- Agent 重启或会话中断后，难以恢复到正确阶段
- 后续做审计、报表、复盘时只能依赖聊天记录，成本高且不可靠

因此，飞书落地时应当把“聊天界面”和“系统状态”分离：

- 飞书主群负责告警入口和状态播报
- 飞书 Thread 负责单事件协作
- SQLite 负责保存事件状态、时间线、审批和审计

这个拆分比“所有内容都堆到一个会话里”稳定得多，也比一开始就上 Kafka、PostgreSQL、向量库更克制。

## 3. 推荐架构

推荐采用三层结构，而不是仅仅把 Thread 当作 Session。

### 3.1 主群卡片层

主群只承担告警入口和状态同步，不承载完整排障过程。

建议主群展示以下信息：

- 告警名称、级别、命名空间、集群
- 当前状态，例如 `已接管`、`调查中`、`待审批`、`执行中`、`已恢复`
- Thread 跳转入口
- 最终结论摘要和后续动作

这样可以保证主群始终清爽，适合值班场景快速浏览。

### 3.2 Thread 工作层

每一条告警在飞书中创建一个独立 Thread，Thread 作为 incident 的协作空间。这里承载：

- 值班人员与 Agent 的对话
- triage / investigate / remediate 的过程摘要
- 审批交互
- 修复验证结果
- handoff 和 postmortem 入口

Thread 的职责是“面向人协作”，不是长期状态存储。

### 3.3 SQLite 状态层

SQLite 是当前阶段最合适的事实源，用来保存结构化状态。建议至少包含以下表或等价数据模型：

- `incidents`：事件主表
- `incident_events`：事件时间线
- `approvals`：异步审批记录
- `audit_log`：审计日志
- `operation_locks`：并发操作锁
- `cost_records`：模型调用成本

只要 SQLite 中的数据完整，Thread 丢历史、上下文裁剪、Agent 重启都不会导致状态不可恢复。

## 4. 核心设计原则

## 4.1 Thread 不是事实源

不要把 Thread 全量消息直接当作 session 记忆。真正的 session 应当是数据库中的 incident 状态机。

Thread 的价值是：

- 给人看
- 给人回复
- 给人审批和确认

数据库的价值是：

- 给系统恢复
- 给工具编排
- 给审计和统计
- 给 handoff 和 postmortem

因此，Agent 推理时不应直接把整个 Thread 原样塞给模型，而应当由系统动态组装上下文。

## 4.2 上下文按需组装，不吃全量聊天记录

每次调用 LLM 时，建议上下文只包含：

- 用户最新问题
- 当前 incident 摘要
- 最近若干条关键事件
- 当前状态和待办事项
- 必要的工具输出摘要
- 相关 runbook 或 skill 片段

不建议直接喂入完整 Thread 消息历史，否则后期仍会出现：

- Token 失控
- 无关聊天噪音污染推理
- 多轮对话越长越难控

## 4.3 原始大输出不进入主对话

`kubectl describe`、日志、PromQL 结果、events 等通常体量很大。建议处理方式是：

- 先脱敏
- 再摘要提取
- 最终只把结构化结论写入 Thread 和 SQLite timeline

原始输出可以保留在临时文件、对象存储或外部日志系统中，但不要直接灌进飞书对话。

## 5. 推荐工作流

### 5.1 告警进入

1. Alertmanager Webhook 发送告警到 Agent
2. Agent 根据 `alertname + namespace + cluster` 做 dedup
3. 创建 incident 记录
4. 在飞书主群发送告警卡片
5. 为该告警创建独立 Thread
6. 在 incident timeline 中写入 `alert_fired`

### 5.2 Triage

1. Agent 在 Thread 中开始接管
2. 读取相关 Pod、Deployment、Events
3. 查询关键指标和日志
4. 评估影响范围和用户感知
5. 产出结论：继续 investigate 或进入 remediate
6. timeline 写入 `triage_start`、`triage_end`

### 5.3 Investigate

1. 聚合日志、指标、事件
2. 结合 runbook/skill 定位根因
3. 形成候选修复方案
4. 如果需要人工判断，发起审批
5. timeline 写入 `investigate_start`、`investigate_end`

### 5.4 Approval and Remediation

1. 若方案涉及 `k8s_write` 或 `k8s_exec`，进入审批流程
2. 审批状态写入 SQLite，不阻塞其他会话
3. 审批通过后执行修复
4. 修复后重新检查 Pod 状态、日志和指标
5. timeline 写入 `approval_sent`、`approval_received`、`remediate_executed`、`remediate_verified`

### 5.5 Recovery and Close

1. 指标恢复、症状消失后更新状态为 `resolved`
2. 主群卡片同步结果摘要
3. Thread 记录修复结论和后续建议
4. 如有价值，触发 skill/runbook 草稿提取

## 6. Incident 状态机建议

建议引入明确的状态机，而不是靠 Thread 文本判断当前阶段。

推荐状态：

- `new`
- `triaging`
- `investigating`
- `pending_approval`
- `executing`
- `verifying`
- `resolved`
- `abnormal`
- `handoff`

这样做的价值很直接：

- Agent 重启后知道当前卡在哪个阶段
- 审批、执行、验证不会互相混淆
- 交接时新值班人可以直接读取当前状态
- 可以稳定计算 MTTD、采纳率、回滚率等指标

## 7. 飞书落地时的具体建议

### 7.1 主群只放摘要，不放细节

主群卡片建议固定只放这些字段：

- 告警标题
- 严重级别
- 集群 / 命名空间
- 当前状态
- 当前负责人
- Thread 链接
- 最终结论摘要

不要在主群中直接推：

- 大段日志
- 大段 YAML
- 多条 kubectl 输出
- 多轮工具调用中间结果

### 7.2 一个告警一个 Thread

Thread 创建策略建议与 incident 强绑定：

- 一个 firing incident 对应一个 Thread
- 同 dedup key 的重复告警落在已有 incident / Thread 上
- 告警恢复后关闭或归档对应 Thread

这样值班人员的心智模型非常清晰，不需要在主群里猜“这条排障过程是在处理哪个告警”。

### 7.3 Thread 内也只展示摘要

即使已经进入 Thread，也不应把所有原始工具输出完整展示出来。更合适的是：

- 展示关键结论
- 展示下一步动作
- 展示审批状态
- 必要时附加“查看原始输出”引用

### 7.4 语音场景只注入短上下文

飞书语音模式下，建议只给语音消息注入简短的活跃 incident 摘要，例如：

- 当前活跃事件 ID
- 告警名称
- 命名空间
- 当前状态

不要把整段 timeline 或长诊断报告拼到语音转写文本前面，否则会影响识别后的理解和回复质量。

## 8. SQLite 在当前阶段为什么够用

对早期或中期部署来说，SQLite 是合理选择，原因如下：

- 单机部署简单，几乎没有额外运维成本
- WAL 模式可以满足低到中等并发
- 审批、审计、timeline、lock 这些数据量都不大
- 对恢复、查询、导出、测试都足够友好

只要遵守以下原则，SQLite 可以稳定运行较长时间：

- 使用 WAL 模式
- 写入加锁，避免并发写冲突
- 所有关键状态都落库
- 工具层只把 SQLite 当事务状态源，不把内存状态当真相

## 9. 什么时候该从 SQLite 演进出去

以下情况出现时，可以考虑迁移到 PostgreSQL：

- Agent 开始多实例部署
- 单机故障不能接受，需要高可用
- 审批、审计、报表查询量明显增长
- 需要跨区域或跨团队共享事件库

迁移原则建议是：

- 保持 incident / approval / audit 的表结构尽量稳定
- 先抽象存储访问层，再替换后端数据库
- 不要在业务层到处直接写 SQL

在这之前，没有必要为了“看起来更企业级”过早引入重型基础设施。

## 10. 还可以怎样演进

推荐的渐进式路线如下。

### 阶段 1：当前最小可用方案

- 主群卡片 + Thread 协作
- SQLite 事件库存状态
- 基础 triage / investigate / remediate
- 审批、审计、交接、恢复全部落地

### 阶段 2：增强自动化

- 更完善的告警聚合和通知防疲劳
- LLM 不可用时自动切换规则引擎
- 周报、成本和效果度量
- runbook 草稿自动生成与人工审核上线

### 阶段 3：多实例和多团队

- SQLite 迁移到 PostgreSQL
- 工作流改造成更明确的状态驱动系统
- 接入 CMDB / 工单 / 值班系统
- 支持跨群、跨平台、跨团队协作

### 阶段 4：知识闭环

- 从 incident timeline 自动提炼 runbook
- 将有效修复路径沉淀为 skill
- 对高频拒绝、回滚和失败操作形成约束规则
- 引入更细粒度的效果评估

## 11. 不建议当前就采用的方案

### 11.1 完全依赖飞书消息历史

这是最容易做错的地方。只靠聊天记录会导致：

- 状态恢复困难
- 审批审计不可靠
- 上下文越来越长
- 指标和复盘难以结构化

### 11.2 一开始就上完整分布式架构

例如一开始就引入：

- Kafka
- Redis
- PostgreSQL
- 向量数据库
- 复杂工作流引擎

这些并不是不能用，而是当前阶段收益不够，复杂度却会显著上升。AIOps 助手早期最大的难点通常不是吞吐，而是：

- 流程是否可恢复
- 权限边界是否正确
- 审批是否可追溯
- 结论是否稳定可解释

### 11.3 用 RAG 替代事务状态

runbook、postmortem、知识库适合做检索；但 incident 当前状态、审批状态、操作锁、负责人等信息必须使用事务状态存储，不能依赖检索结果。

## 12. 推荐结论

如果要在飞书上落地 AIOps 助手，当前最合适的路径就是：

- `飞书 Thread 级隔离` 解决多告警并发和聊天界面清晰度问题
- `SQLite 事件库` 作为长期记忆、审批审计和恢复的事实源
- `incident 状态机` 作为真实 session，而不是把 Thread 文本当 session
- `按需组装上下文` 控制 Token 和噪音
- `主群与 Thread 都展示摘要，不直接展示大输出`

这条路线的优点是清晰、可控、可恢复、便于迭代，而且不会过早进入过度设计。

## 13. 建议的后续实施项

建议按以下顺序推进：

1. 固化 incident 与 Thread 的一一映射关系
2. 统一 incident 状态机和 timeline event 枚举
3. 让所有 triage / investigate / remediate 动作都先写 timeline 再发消息
4. 主群卡片只显示摘要状态，不显示排障细节
5. Thread 回复统一走摘要化输出，原始大输出只保留引用
6. 启用审批、审计、交接、恢复的全链路落库
7. 后续再考虑 PostgreSQL、多实例和知识闭环
