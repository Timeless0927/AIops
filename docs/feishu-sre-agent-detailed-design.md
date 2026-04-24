# 飞书落地 AIOps SRE Agent 详细设计

## 1. 文档目的

本文档面向实现和运维团队，给出 AIOps SRE Agent 在飞书场景落地的详细设计。目标不是重复高层愿景，而是把方案收敛为可以直接实施的系统设计。

本文档重点回答以下问题：

- 为什么采用 `飞书 Thread 级隔离 + SQLite 事件库` 的架构
- 各个系统组件分别负责什么
- 告警、排障、审批、修复、恢复、交接如何串起来
- Thread、Session、Incident 三者的关系如何定义
- 数据表、状态机、消息格式如何设计
- 出现中断、重复告警、模型失败、审批超时等异常时如何处理
- 当前阶段如何控制复杂度，以及未来如何平滑演进

## 2. 设计结论

当前阶段的推荐落地方案是：

- 飞书主群作为统一告警入口和状态广播面
- 每个告警创建独立 Thread，作为单 incident 的协作空间
- SQLite 作为长期记忆、事务状态和审计事实源
- Agent 每次推理时按需组装 incident 上下文，而不是直接喂完整 Thread 历史
- 审批、审计、交接、恢复全部以结构化记录落库

这套方案可以概括为：

`Thread 负责人机协作，SQLite 负责系统真相，incident state 负责真实 session。`

## 3. 核心概念定义

为了避免实现中概念混乱，先统一术语。

### 3.1 Alert

外部监控系统发来的原始告警对象，通常来自 Alertmanager，包含：

- `alertname`
- `severity`
- `namespace`
- `cluster`
- `description`
- `status`
- 其他 labels / annotations

Alert 是输入，不是系统内的长期主对象。

### 3.2 Incident

Incident 是系统内部的排障主对象，代表一条正在处理或已经完成的运维事件。每个 incident 具有：

- 唯一 ID
- 当前状态
- 当前负责人
- 所属 cluster / namespace
- 创建时间、解决时间
- 简要摘要
- 完整 timeline

一个 incident 可以由一条或多条重复告警触发，但在系统内部应当只有一个统一状态机。

### 3.3 Thread

Thread 是飞书中的交互容器，用于承载某个 incident 的对话和状态摘要。Thread 是界面层对象，不是事实源。

### 3.4 Session

Session 不是飞书线程本身，而是 Agent 在处理某个 incident 时所使用的一组运行态上下文。这个上下文应由系统根据 incident state 和最近关键事件动态组装。

### 3.5 Timeline Event

Timeline Event 是 incident 生命周期中发生的结构化事件，例如：

- `alert_fired`
- `triage_start`
- `triage_end`
- `investigate_start`
- `approval_sent`
- `approval_received`
- `remediate_executed`
- `resolved`

timeline 是后续恢复、复盘、审计和指标计算的基础。

## 4. 架构总览

系统建议采用四层结构。

### 4.1 接入层

负责接收飞书消息和外部告警，主要组件包括：

- 飞书 Bot / Gateway
- Alertmanager Webhook
- 语音消息转写入口
- Hook 生命周期入口

### 4.2 编排层

负责任务分流、上下文组装和流程推进，主要包括：

- Hermes Agent Loop
- Session 上下文构建器
- Triage / Investigate / Remediate Skill
- Hook 管理器
- 审批和恢复协调逻辑

### 4.3 工具层

负责执行面向 SRE 的实际工具调用，主要包括：

- K8s 读写执行工具
- Prometheus / Loki 查询工具
- Query Guard / Permission Guard
- Notification / Cost / Metrics / Audit 等支撑工具

### 4.4 状态层

负责持久化和恢复，主要包括 SQLite 中的：

- incidents
- incident_events
- approvals
- audit_log
- operation_locks
- cost_records
- rejection_lessons

### 4.5 系统运行模式

除了 incident 主状态，系统还需要一个独立的全局运行模式，用于表达平台级降级，而不是把全局问题直接写回每个 incident。建议定义：

- `system_mode=normal`
- `system_mode=degraded`
- `system_mode=read_only`

使用原则：

- 数据库、飞书回调、审批中心等全局依赖异常时，优先切换 `system_mode`
- 只有某个 incident 因局部条件不足无法推进时，才考虑将其置为 `abnormal`
- 不建议把系统级降级直接映射为所有 incident 的主状态变化

## 5. 关键设计原则

### 5.1 聊天记录不是事实源

如果把飞书 Thread 全量历史当作长期记忆，会出现以下问题：

- 历史越长，模型上下文越不可控
- 人工插话、闲聊、确认消息会污染推理
- 系统重启后很难精确恢复到操作阶段
- 审批和审计无法直接形成结构化记录

因此必须把“消息展示”和“系统状态”解耦。

### 5.2 所有关键动作先落库，再对外展示

建议遵循如下原则：

1. 工具执行或状态变更发生
2. 先写 timeline / status / audit
3. 再推送主群卡片或 Thread 摘要

这样即使飞书发送失败，系统仍然保留完整事实。

### 5.3 Thread 只承载摘要，原始大输出不直出

K8s describe、日志、metrics 明细通常很长，建议统一处理链路：

1. 原始输出获取
2. 安全脱敏
3. 结构化提取
4. 写入 timeline 摘要
5. 只将摘要发送到 Thread

当前阶段建议把原始大输出的落点也写死：

- 默认不把原始大输出作为长期事实源保存到 SQLite
- 原始输出只保留在临时文件或短期缓存中，例如 24 小时内可追溯
- timeline 只保存摘要、引用 ID 和必要的安全元数据
- 超过保留期后自动清理，避免把高风险原文长期沉淀在系统中

### 5.4 同一 incident 的写操作必须串行

涉及修复、回滚、重启、扩容等实际变更时，必须对目标资源加 operation lock，避免：

- 多个 Agent 或多个值班人同时执行冲突操作
- 审批通过后重复执行
- 重复告警触发并发修复

### 5.5 权限校验必须在工具层完成

Prompt 中的权限约束只用于提示，不可作为真正安全边界。真正的工具权限必须在 Python 代码中硬检查。

## 6. 飞书交互模型

### 6.1 主群职责

主群只承担以下职责：

- 展示新告警卡片
- 展示状态变更播报
- 提供跳转到 Thread 的入口
- 展示最终结论或升级说明

主群不承担：

- 完整排障过程输出
- 大段工具调用结果
- 长日志
- 逐步 reasoning 文本

### 6.2 Thread 职责

每个 incident 对应一个 Thread。Thread 用于：

- 值班人与 Agent 互动
- Agent 输出 triage / investigate / remediate 摘要
- 触发审批、确认、继续调查
- 展示最新状态和下一步建议

### 6.3 语音消息职责

语音消息适合：

- 查询当前事件状态
- 快速询问集群是否异常
- 获取精简结论
- 触发简单确认

语音交互不适合承载长细节，因此语音场景下必须走摘要化上下文和摘要化输出。

## 7. Session 设计

### 7.1 正确的 Session 定义

在飞书落地中，Session 不应等同于“某个 Thread 的所有消息历史”，而应当定义为：

- 当前 incident 的结构化摘要
- 当前状态
- 最近关键 timeline events
- 当前操作者身份
- 当前审批 / 锁 / 待办信息
- 用户最新输入

### 7.2 推理上下文构建建议

每次进入模型推理前，建议构造如下上下文块：

1. 操作者信息
2. 当前 incident 摘要
3. 当前状态和推荐动作
4. 最近 3 到 10 条关键 timeline
5. 必要的工具结果摘要
6. 用户最新输入

这样做的收益是：

- token 成本可控
- 推理上下文更稳定
- 中断恢复更容易
- 更适合后续切到不同模型

### 7.3 不建议的做法

不要把以下内容作为默认上下文全量送入模型：

- 整个 Thread 完整历史
- 原始日志全文
- 完整 YAML 清单
- 多轮重复的排障对话

## 8. 生命周期与状态机设计

### 8.1 Incident 状态枚举

推荐状态如下：

- `new`
- `triaging`
- `investigating`
- `pending_approval`
- `executing`
- `verifying`
- `resolved`
- `closed`
- `abnormal`

### 8.2 状态说明

`new`
- 新建 incident，刚由告警创建，还未开始处理。

`triaging`
- 正在做快速分类和影响判断。

`investigating`
- 已确认需要进一步诊断，正在分析日志、指标和事件。

`pending_approval`
- 已形成修复动作，但等待人工审批。

`executing`
- 审批已通过，正在执行修复动作。

`verifying`
- 修复动作已完成，正在核验恢复效果。

`resolved`
- 已恢复，症状已消失，但仍允许在 reopen 窗口内重新打开。

`closed`
- 已完成归档关闭，不再接受 reopen，后续同类告警应创建新 incident。

`abnormal`
- 出现异常中断、执行状态不一致或需要人工介入。

### 8.3 典型状态迁移

正常路径：

`new -> triaging -> investigating -> pending_approval -> executing -> verifying -> resolved -> closed`

无需修复时：

`new -> triaging -> resolved -> closed`

自动修复无需审批时：

`new -> triaging -> investigating -> executing -> verifying -> resolved -> closed`

reopen 路径：

- `resolved -> triaging`

异常路径：

- `executing -> abnormal`
- `pending_approval -> abnormal`

### 8.4 状态迁移约束

建议至少实现以下约束：

- `resolved` 之后不允许继续执行修复，除非满足 reopen 规则
- `closed` 状态下不允许 reopen，只能创建新 incident
- `pending_approval` 状态下不能直接并发执行多个写操作
- `executing` 状态必须存在有效 operation lock
- 交接完成后当前 operator 不再作为默认负责人

## 9. 数据模型设计

以下数据模型以 SQLite 为基线。

### 9.1 incidents

建议字段：

- `id`
- `alert_name`
- `namespace`
- `cluster`
- `status`
- `operator`
- `created_at`
- `resolved_at`
- `closed_at`
- `summary`
- `platform`
- `chat_id`
- `root_message_id`
- `thread_id`
- `status_card_message_id`
- `dedup_key`
- `dedup_key_version`
- `reopen_count`

作用：

- 保存 incident 主信息
- 提供当前状态与负责人查询
- 作为其余表的关联主键
- 建立 incident 与飞书主群卡片、根消息、Thread 的稳定映射
- 支撑恢复补发、主群状态更新、Thread 追写和 reopen 判定

当前项目已有 `toolsets/incident_store.py`，但现有实现只包含 `id / alert_name / namespace / cluster / status / created_at / resolved_at / summary / operator` 等基础字段。因此飞书落地时不能直接假设 Thread 绑定、状态卡片更新、reopen 和消息补偿已经具备，需要先扩展 schema，并提供向后兼容迁移。

### 9.2 incident_events

建议字段：

- `id`
- `incident_id`
- `event_type`
- `timestamp`
- `tool_name`
- `input_summary`
- `output_summary`
- `metadata_json`

作用：

- 形成完整 timeline
- 供恢复、复盘、指标、skill 提取使用

### 9.3 approvals

建议字段：

- `id`
- `incident_id`
- `operation_type`
- `command`
- `context_json`
- `namespace`
- `requester`
- `approver`
- `status`
- `risk_level`
- `denial_reason`
- `approval_message_id`
- `created_at`
- `decided_at`
- `executed_at`
- `result_json`

作用：

- 非阻塞审批
- 审批状态恢复
- 拒绝学习和统计
- 将审批结果与 incident、Thread 通知、审计记录稳定关联

当前项目已有 `toolsets/approval_async.py`，但现有实现尚未持久化 `incident_id` 和 `approval_message_id`。因此审批模块目前只能表达“某个操作等待审批”，还不能可靠表达“某个 incident 在某个 Thread 中等待哪条飞书审批消息”。飞书落地前必须补齐这两个关联字段。

### 9.4 audit_log

建议字段：

- `id`
- `who`
- `what`
- `when_ts`
- `cluster`
- `namespace`
- `trigger`
- `tool_level`
- `tool_name`
- `dry_run`
- `result`
- `approval_by`
- `approval_at`
- `rollback`
- `snapshot_path`
- `incident_id`

作用：

- 合规审计
- 回滚率计算
- 人工追责与复盘

### 9.5 operation_locks

建议字段：

- `resource_key`
- `session_id`
- `acquired_at`
- `expires_at`

作用：

- 保证写操作互斥
- 避免同一资源被重复修复

### 9.6 cost_records

建议字段：

- `timestamp`
- `incident_id`
- `model`
- `input_tokens`
- `output_tokens`
- `estimated_cost`
- `session_id`

作用：

- 日预算控制
- 单 incident 成本统计
- 触发降级策略

### 9.7 message_deliveries

建议字段：

- `id`
- `incident_id`
- `approval_id` 可选
- `target_type`
  例如 `status_card`、`thread_summary`、`approval_notice`、`approval_result`
- `platform`
- `chat_id`
- `thread_id` 可选
- `target_message_id`
- `delivery_status`
- `delivery_attempts`
- `last_delivery_error`
- `last_delivery_at`
- `payload_hash`
- `created_at`
- `updated_at`

作用：

- 跟踪主群卡片、Thread 摘要、审批通知等多对象消息投递状态
- 为补偿重试和恢复补发提供幂等依据
- 避免把多个消息对象的补偿字段硬塞进 `incidents` 主表

## 10. 告警进入流程详细设计

### 10.1 输入来源

输入来源为 Alertmanager 标准 webhook：

- `alerts[]`
- `labels`
- `annotations`
- `status`

### 10.2 处理步骤

1. 接收 webhook 请求
2. 可选校验签名
3. 提取 `alertname / severity / namespace / cluster / description`
4. 调用 dedup 模块判断是否需要处理
5. 若为重复且在窗口内，只更新 group 计数，不创建新 incident
6. 若应处理，创建 incident
7. 写入 `alert_fired` timeline
8. 在主群发卡片
9. 创建或关联 Thread
10. 在 Thread 中触发 triage

### 10.3 Dedup Key 建议

推荐使用：

`alertname|namespace|cluster`

必要时可进一步加入：

- workload 名称
- pod 名称
- alert fingerprint

### 10.4 风暴保护

在 1 分钟滑动窗口内告警量超过阈值时：

- 非 critical 告警进入 digest 队列
- critical 告警仍进入主流程
- 主群只播报摘要，不对每条告警逐条刷屏

### 10.5 Resolved 后再次触发的 Reopen 规则

系统必须明确区分“重复告警”和“重新打开旧事件”。建议规则如下：

权限和触发来源建议明确如下：

- Alertmanager 的重复 firing 满足规则时，可以自动 reopen
- 人工在 Thread 中请求 reopen 时，必须经过权限校验
- `closed` 之后默认不允许手动 reopen，管理员也应优先创建新 incident，而不是强行复用旧 incident


- 若同一 `dedup_key` 在 incident 处于 `resolved` 后、且仍在 reopen 窗口内再次触发，则 reopen 原 incident
- reopen 原 incident 时，优先复用原 Thread，并在原 Thread 中追加“再次触发”摘要
- 主群优先更新原状态卡片，而不是再发一张新的告警卡片
- incident 的 `reopen_count` 自增，并新增 `reopened` timeline event
- 若已超过 reopen 窗口，或 incident 已进入 `closed`，则必须创建新 incident

默认建议：

- reopen 窗口可配置，例如 30 分钟到 24 小时之间
- `resolved` 表示可 reopen，`closed` 表示不可 reopen
- 指标统计中 reopen 仍计入原 incident 生命周期，新建 incident 则单独统计

这样可以统一 dedup、MTTD / MTTR、复盘归因和飞书侧展示语义。

## 11. Triage 详细设计

### 11.1 目标

Triage 的目标不是给出完整根因，而是快速回答：

- 告警是否真实
- 影响范围多大
- 是否用户可感知
- 是否已知模式
- 下一步是 investigate 还是直接 remediate

### 11.2 建议工具调用顺序

1. `k8s_read` 获取相关 pod / deployment / events
2. `prometheus_query` 获取关键指标现状
3. 必要时 `loki_query` 获取少量错误日志
4. 汇总为简短分类结论

### 11.3 输出格式建议

Thread 中建议输出：

- 当前判断：真实故障 / 噪声 / 待确认
- 影响范围：单服务 / 单命名空间 / 集群级
- 当前风险：高 / 中 / 低
- 下一步：继续调查 / 请求审批 / 持续观察

### 11.4 对主群的回写

若 triage 完成后结论明确，主群卡片可更新为：

- `已接管`
- `影响服务：xxx`
- `建议下一步：调查中`

## 12. Investigate 详细设计

### 12.1 目标

Investigate 负责定位根因，并形成可信的修复建议。

### 12.2 建议分析维度

- 指标趋势
- 错误日志
- K8s events
- 资源配置与限制
- 最近变更
- 历史 runbook 匹配

### 12.3 产出要求

Investigate 结束后应至少形成：

- 根因候选列表
- 最可能根因
- 修复动作建议
- 风险说明
- 是否需要审批

### 12.4 与 Skill 的关系

Investigate 可以优先匹配 runbook skill。例如：

- Pod CrashLoopBackOff
- Node NotReady
- High Memory
- Certificate Expiry
- PVC Full

如果命中已有 runbook，应优先按结构化步骤执行，而不是完全自由推理。

## 13. 审批与修复详细设计

### 13.1 审批原则

所有写操作和 exec 操作都不能只靠 LLM 决定，必须经过权限规则和审批规则判定。

建议优先顺序：

1. `permission_guard` 判断操作者是否允许使用该工具
2. `approval_rule` 判断是否需要审批
3. 若需要审批，写入 approvals 并更新 incident 状态
4. 审批通过后才允许执行

### 13.2 审批不阻塞 Session

审批请求发送后，不应阻塞整条 Session。系统应当：

- 将 incident 状态设为 `pending_approval`
- 写入 `approval_sent`
- 在 Thread 中通知“等待审批”
- 审批通过后以异步事件恢复流程

### 13.2.1 审批与操作锁的时序规则

审批和锁的先后顺序需要固定，否则会出现“审批结果通过，但执行阶段抢锁失败”的体验问题。当前阶段建议采用：

1. 先完成权限校验和审批判定
2. 审批通过后，再尝试获取执行锁
3. 获取锁成功后才允许进入 `executing`
4. 获取锁失败时，不直接执行修复，而是返回“资源正在被其他会话处理”
5. 锁失败结果写入 timeline 和 audit，并在 Thread 中提示冲突

采用这个策略的原因是：

- 审批是对操作意图的授权，不等于执行资格已经最终占有
- 执行锁是资源层面的互斥控制，必须以实际执行时刻为准
- 若审批通过但锁失败，系统必须明确告诉值班人“审批有效，但当前资源已被其他流程占用”

后续如果并发冲突频繁，再考虑升级为“预占逻辑锁位 + 审批通过后升级为执行锁”的两阶段模型。

### 13.3 高危操作识别

以下类型建议默认为高危：

- delete pod / deployment / pvc
- exec 进入容器执行命令
- node 级别操作
- 强制重启或资源回收

这些操作应要求更高等级审批，必要时只能由 `can_approve` 角色执行。

### 13.4 修复执行策略

建议修复时遵循以下顺序：

1. 能 dry-run 则先 dry-run
2. 加 operation lock
3. 执行最小必要动作
4. 记录 audit
5. 更新 timeline
6. 进入 verifying

## 14. 验证与关闭详细设计

### 14.1 验证目标

验证阶段要确认的不是“命令执行成功”，而是“症状是否消失”。

### 14.2 建议验证维度

- Pod 是否 Ready
- Restart count 是否停止增长
- 关键错误日志是否消失
- 核心指标是否回落到正常范围
- 告警是否恢复

### 14.3 关闭条件

建议同时满足以下条件才标记 `resolved`：

- 修复动作成功执行
- 验证通过
- 关键症状消失
- 无待处理审批和锁

### 14.4 主群更新格式

主群最终状态建议为：

- `已恢复`
- `根因：xxx`
- `修复动作：xxx`
- `恢复时间：xxx`

## 15. 交接设计

默认情况下，handoff 更适合作为 timeline event，而不是 incident 主状态。只有在系统要求“等待新负责人确认接管”时，才建议短暂进入显式的待交接阶段。当前阶段建议采用更克制的实现：

- 交接动作默认只更新负责人、写 timeline、写 audit、发送交接说明
- incident 主状态通常保持不变，例如继续停留在 `investigating`
- 只有明确存在“暂停流程等待接手确认”的产品需求时，才再引入专门的交接中间态

### 15.1 触发场景

以下情况适合触发 handoff：

- 值班轮换
- 当前负责人离线
- 需要更高权限的运维介入
- 事件升级到专项团队

### 15.2 交接动作

交接时系统应自动完成：

1. 获取所有活跃 incident
2. 拉取每个 incident 最近 3 条关键事件
3. 生成交接摘要
4. 更新 operator
5. 记录 audit
6. 在 Thread 中发送交接说明

### 15.3 交接摘要建议字段

- incident_id
- alert_name
- namespace / cluster
- 当前状态
- 最近动作
- 当前待办
- 风险点

## 16. 恢复设计

### 16.1 恢复触发时机

系统启动时应自动执行恢复逻辑，扫描：

- 活跃 incident
- 待审批记录
- 过期锁
- 执行中但无锁的异常 incident

### 16.2 恢复逻辑建议

- `pending_approval`：重新确认审批状态，过期则标记异常或 expired
- `investigating`：加入 interrupted 列表，等待恢复
- `executing` 但无有效锁：标记 `abnormal`
- 清理过期审批和锁

### 16.3 恢复输出

恢复逻辑应输出结构化结果，至少包括：

- `pending_approval`
- `interrupted`
- `abnormal`
- `expired_approvals`
- `expired_locks`

### 16.4 消息同步恢复

恢复逻辑不应只修复数据库状态，还应检查飞书界面是否与数据库真相一致。建议恢复阶段额外扫描：

- 未同步成功的主群状态卡片
- 未同步成功的 Thread 状态摘要
- 未送达的审批通知
- 未送达的审批结果通知

为支持这类恢复，建议对消息投递状态记录以下字段或等价结构：

- `delivery_status`
- `delivery_attempts`
- `last_delivery_error`
- `last_delivery_at`

恢复补发时必须满足两个约束：

- 补发动作幂等，不能因为重试造成重复卡片或重复 Thread 消息
- 补发以数据库中的最新状态为准，不补发已经过期的旧摘要

这样才能保证“数据库是真相”进一步演进为“飞书界面最终一致”。

## 17. 审计与合规设计

### 17.1 必须审计的动作

建议对以下动作统一写审计日志：

- 所有 SRE 工具调用
- 所有写操作与 exec
- 审批请求、审批通过、审批拒绝
- 回滚动作
- 运维交接
- 系统自动恢复动作

### 17.2 审计最小字段

每条审计建议至少包含：

- 谁执行的
- 执行了什么
- 在什么时间
- 针对哪个 namespace / cluster
- 结果是什么
- 是否经过审批
- 是否涉及回滚
- 属于哪个 incident

## 18. 语音场景专项设计

### 18.1 语音上下文增强

若用户消息来自语音转写，建议只在前缀中注入短 incident 摘要，例如：

`[当前活跃事件: inc_xxx PodCrashLoop in default, status=investigating]`

不要注入长 timeline。

补充约束建议如下：

- 默认最多注入 1 到 3 条活跃 incident，避免语音前缀过长
- 优先级依次为：当前 Thread 对应 incident、当前操作者负责的 incident、当前群最近活跃的 incident
- 若没有活跃 incident，则不注入任何 incident 前缀，只保留原始转写文本
- 注入内容只保留 `incident_id`、`alert_name`、`namespace`、`status`，不包含长摘要

### 18.2 语音输出格式

语音回复应满足：

- 只说结论，不读原始命令输出
- 口语化中文表达
- 最后给出明确的下一步建议
- 需要时加 `[[audio_as_voice]]` 标记触发 TTS

### 18.3 语音摘要工具使用场景

以下内容应先经 `sre_voice_summary` 压缩再播报：

- triage 结果
- investigate 结论
- 每日摘要
- 周报
- 恢复通知

## 19. 成本与降级设计

### 19.1 成本控制

建议同时控制：

- 日预算
- 单 incident 预算
- 高成本模型调用频率

达到阈值时，可以采取：

- 改用便宜模型
- 缩短上下文
- 降级到规则引擎
- 仅输出摘要，不继续深入分析

### 19.2 LLM 降级

当 LLM 不可用或超预算时，应根据 `fallback_rules` 返回可执行的规则化建议，而不是直接失败。

例如：

- Pod CrashLooping -> 输出 describe + logs 的固定排查动作
- Node NotReady -> 输出 describe node + events 的固定排查动作

### 19.2.1 数据库不可用时的统一降级模式

数据库不可用时，系统应进入统一的 `degraded` 模式，而不是让不同模块各自定义行为。建议统一约束如下：

- 禁止所有会改变 incident 状态或执行实际修复的高风险动作
- 允许只读查询工具继续工作，但明确提示“结果不会被持久化”
- 主群和 Thread 只发送降级提示，不发送会造成误导的状态更新
- 审批、审计、交接、恢复等依赖数据库一致性的流程全部暂停
- 数据库恢复后，由恢复逻辑统一校正状态并补发必要消息

这样可以避免系统在部分功能失效时继续输出伪进展。

### 19.3 通知防疲劳

对主群和运维人员的通知建议增加：

- severity 过滤
- dedup window
- quiet hours
- max per hour
- daily digest

## 20. 指标与效果度量设计

建议每周至少统计以下指标：

- 处理 incident 数量
- 平均 MTTD
- 修复方案采纳率
- 回滚率
- 平均成本
- 高拒绝率操作类型

这些指标既用于证明价值，也用于发现哪些自动化策略不靠谱。

### 20.0 指标口径定义

为了避免不同实现版本统计口径漂移，建议当前阶段先固定如下定义：

- `MTTD`：从 `alert_fired` 到 `triage_start` 的秒数
- `方案采纳率`：按 approvals 中 `approved / (approved + denied)` 计算
- `回滚率`：按 audit 记录粒度统计 `rollback=1 / 全部修复相关审计记录`
- `reopen` incident：默认仍归入原 incident 生命周期，不单独拆成新事件统计
- `平均成本`：按 `cost_records` 中同统计周期内的 `estimated_cost` 总和 / incident 数量计算

如果后续产品或运营侧要求变更口径，必须同时更新周报逻辑和文档定义。

## 20.1 外部平台假设

本文档默认以下飞书能力在目标环境中可用：

- 机器人具备主群发消息、回调接收、Thread 回复等基础能力
- 告警卡片可以被稳定更新，或至少可以通过消息 ID 做幂等替换
- 机器人能够区分主群根消息、Thread 消息和审批通知消息

如果目标环境不满足这些前提，需要采用降级方案：

- 若不支持 Thread，则退化为“主群卡片 + 外链工单 / 外链事件详情页”
- 若卡片不可更新，则改为发送新的状态播报消息，并在数据库中保留消息链路映射
- 若回调能力受限，则审批和交接流程需要转移到外部页面或审批系统

因此，Thread 是推荐方案的关键体验组件，但不应被当作唯一可运行前提。

## 21. 消息模板建议

### 21.1 主群新告警卡片

建议字段：

- 标题：`[critical] KubePodCrashLooping`
- 集群：`prod-cluster`
- 命名空间：`payments`
- 当前状态：`new`
- 负责人：`待分配`
- 操作：`查看 Thread`

### 21.2 Thread 初始接管消息

建议格式：

```text
已接管该事件。
当前正在执行 triage，先检查 Pod 状态、关键指标和最近事件。
如需修复动作，我会先给出方案并等待审批。
```

### 21.3 调查结论消息

建议格式：

```text
初步结论：payments-api 的 Pod 持续重启，最可能原因是内存不足触发 OOMKilled。
影响范围：当前仅影响 payments 命名空间下该服务。
建议下一步：先提升内存 limit 并观察 10 分钟。
```

### 21.4 待审批消息

建议格式：

```text
建议执行修复动作：kubectl scale deployment/payments-api --replicas=4 -n payments
风险等级：standard
当前状态：等待审批
```

### 21.5 修复完成消息

建议格式：

```text
修复动作已执行完成，正在验证恢复效果。
当前检查项包括 Pod Ready、错误日志、核心指标和告警恢复状态。
```

## 22. 异常与边界情况设计

### 22.1 重复告警

同一 dedup key 的告警在窗口内重复到达时：

- 不新建 incident
- 更新告警组计数
- 必要时在当前 Thread 中追加“重复触发”摘要

### 22.2 审批超时

审批超时后：

- approvals 标记为 `expired`
- incident 可转 `abnormal` 或继续等待人工处理
- Thread 中提示审批过期

### 22.3 执行中断

若修复执行中 Agent 重启或进程退出：

- 启动恢复流程
- 检查 incident 是否有 lock
- 若状态为 `executing` 但无 lock，标记 `abnormal`

### 22.4 飞书消息发送失败

飞书发送失败不应影响系统事实写入。做法：

- 状态和 timeline 已落库
- 将消息投递失败记为 warning 或 audit
- 后续可重试同步 Thread 或主群

进一步建议把消息投递当成独立的补偿链路处理：

- 为主群卡片和 Thread 摘要分别记录 `delivery_status`
- 每次投递失败都增加 `delivery_attempts` 并记录 `last_delivery_error`
- 重试逻辑必须按消息类型和目标对象做幂等控制
- 若当前状态已经推进到更新阶段，补发时只发送最新状态，不回放过时消息

### 22.5 数据库暂不可用

SQLite 不可用时，不应让各模块各自定义行为，而应统一进入 `degraded` 模式。建议统一处理如下：

- 禁止所有会改变 incident 状态或执行实际修复的高风险动作
- 允许只读查询继续执行，但明确提示结果不会被持久化
- 主群和 Thread 只发送降级提示，不发送会误导值班人的状态更新
- 审批、审计、交接、恢复等依赖数据库一致性的流程全部暂停
- 数据库恢复后，由恢复逻辑统一补发必要消息并校正状态

## 23. 安全设计

### 23.0 回调幂等约束

所有来自飞书的按钮回调、消息回调、审批回调都必须具备幂等控制。建议统一约束如下：

- 每个回调请求必须携带幂等键或可稳定推导的事件 ID
- 同一回调只允许成功推进一次状态
- 对已处理的重复回调，返回当前状态，不重复执行副作用
- 所有会触发状态推进或执行动作的回调都必须写入 timeline

这条约束对审批通过、审批拒绝、Thread 内确认按钮尤其关键。


### 23.1 工具权限边界

权限至少包含：

- 操作者身份
- 可访问 namespace 列表
- 可使用工具列表
- 是否可审批

### 23.2 查询安全护栏

PromQL 和 LogQL 查询需加统一 guard：

- 默认时间窗口
- 最大 limit
- 禁止全量匹配
- 查询超时
- 慢查询告警

### 23.3 命令执行安全

K8s 命令必须分类为：

- read
- write
- exec
- forbidden

高危操作必须显式升级审批级别。

### 23.4 敏感信息处理

以下内容不得原样进入对话：

- Secret `data`
- API key / token
- kubeconfig 中的敏感字段
- 其他运维凭据



## 24. 关键时序流程

本节把前文分散描述的流程收敛成可落地的时序规则，方便实现团队按统一顺序编排。

### 24.1 告警进入到 Thread 建立

建议时序如下：

1. Alertmanager 向 webhook 发送 firing 告警
2. Webhook 校验请求并提取标准字段
3. Dedup 模块判断是重复告警、reopen 还是新 incident
4. 若为新 incident，则创建 incidents 主记录
5. 写入 `alert_fired` timeline event
6. 发送或更新主群告警卡片
7. 建立 Thread 或关联已有 Thread
8. 在 Thread 中发送“已接管”摘要
9. 触发 triage 流程

关键约束：

- 先写数据库，再发送飞书消息
- 主群卡片和 Thread 都必须保留外部对象映射
- 若飞书发送失败，不回滚数据库，只进入消息补偿链路

### 24.2 Triage 到 Investigate

建议时序如下：

1. incident 状态从 `new` 进入 `triaging`
2. 记录 `triage_start`
3. 调用 `k8s_read`、`prometheus_query`，必要时调用 `loki_query`
4. 生成影响范围和风险判断
5. 记录 `triage_end`
6. 若故障已消失或确认是噪声，可直接进入 `resolved`
7. 若需要深入排查，则进入 `investigating`
8. 在 Thread 和主群卡片中写入摘要化结论

关键约束：

- Triage 只回答是否真实、影响范围和下一步，不追求完整根因
- 若输出过长，必须先摘要再进入对话
- 若命中已有 runbook 模式，应优先转入结构化 investigate 流程

### 24.3 Investigate 到 Approval

建议时序如下：

1. incident 状态进入 `investigating`
2. 记录 `investigate_start`
3. 汇总日志、指标、事件和配置证据
4. 输出最可能根因、修复建议和风险说明
5. 记录 `investigate_end`
6. 判断修复动作是否需要审批
7. 若不需要审批，直接进入执行准备阶段
8. 若需要审批，则创建 approval 记录并将 incident 置为 `pending_approval`
9. 在 Thread 中发送待审批摘要

关键约束：

- 审批是对修复意图的授权，不等于已经取得执行资格
- 审批记录必须与 incident、operator、namespace、risk_level 建立可追溯关联
- 审批通知发送失败时，approval 记录仍然有效，但需要补偿发送

### 24.4 Approval 到 Execute

建议时序如下：

1. 审批通过后，系统接收 approval resolved 事件
2. 记录 `approval_received`
3. 尝试获取 operation lock
4. 获取锁成功后，incident 才能进入 `executing`
5. 记录 `remediate_executed` 前的执行计划摘要
6. 执行 dry-run 或最小必要修复动作
7. 写入 audit
8. 执行结束后释放锁或转换为验证期占用策略
9. incident 进入 `verifying`

关键约束：

- 审批通过但抢锁失败时，不得执行修复
- 锁冲突必须对用户明确可见，不能静默吞掉
- 高危操作的审批级别必须在执行前再次校验，不能只依赖历史上下文

### 24.5 Verify 到 Resolve / Close

建议时序如下：

1. incident 进入 `verifying`
2. 检查 Pod Ready、错误日志、核心指标和告警状态
3. 若症状消失，记录 `remediate_verified`
4. incident 进入 `resolved`
5. 在 reopen 窗口结束后再进入 `closed`
6. 更新主群卡片和 Thread 结论摘要
7. 如有价值，触发 postmortem 或 skill 提取流程

关键约束：

- `resolved` 表示故障已恢复，但仍允许 reopen
- `closed` 表示归档完成，不再与后续 firing 告警复用
- 主群最终消息只能展示摘要，不能回灌长工具输出
- `resolved -> closed` 不应依赖人工记忆，应由定时任务或恢复流程根据 reopen 窗口和消息补偿状态自动收敛
- 具体收敛条件建议写死为：reopen 窗口结束、消息补偿完成、无待处理审批、无待人工确认事项
- 允许管理员在特殊情况下人工提前关闭，但必须写 audit，并且关闭后强制后续告警创建新 incident

### 24.6 启动恢复时序

建议时序如下：

1. Agent 启动后执行 recovery hook
2. 扫描活跃 incidents、approvals、locks 和消息补偿状态
3. 对 `pending_approval` 重新确认审批结果
4. 对 `executing` 但无锁的 incident 标记为 `abnormal`
5. 清理过期 approval 和 lock
6. 扫描未同步完成的主群卡片、Thread 摘要和审批通知
7. 以数据库最新状态为准补发必要消息
8. 输出恢复结果摘要供运维确认

关键约束：

- 恢复只修正状态，不重复执行修复动作
- 恢复补发必须幂等
- 任何无法自动判断的一致性问题应转 `abnormal` 并等待人工介入

## 25. 状态与字段规范

本节用于约束哪些信息应该建模为 incident 主状态，哪些应该作为子状态、补偿状态或事件字段存在，避免状态机无限膨胀。

### 25.1 incident 主状态

incident 主状态只表达“当前事件处于哪个主阶段”，建议限定为：

- `new`
- `triaging`
- `investigating`
- `pending_approval`
- `executing`
- `verifying`
- `resolved`
- `closed`
- `abnormal`

使用原则：

- 主状态必须足以驱动恢复逻辑和主流程编排
- 主状态必须能被主群卡片和 Thread 摘要稳定展示
- 不应把所有边缘结果都提升为主状态

### 25.2 approval 子状态

approval 相关结果不应直接膨胀 incident 主状态，而应保留在 approvals 记录中。建议状态包括：

- `pending`
- `approved`
- `denied`
- `expired`
- `executed`

使用原则：

- `denied` 和 `expired` 默认不升格为 incident 主状态
- incident 可根据策略继续保持 `pending_approval`、转为 `abnormal`，或等待人工处理
- 所有审批结果都必须写 timeline，便于恢复和统计

### 25.2.1 为什么 `denied` / `expired` 不升格为 incident 主状态

审查意见中提到 `rejected`、`expired` 是否需要单独建模，这个问题是成立的，但当前更合适的落点不是 incident 主状态，而是 approval 子状态，原因如下：

- 它们描述的是审批子流程结果，不是 incident 的主阶段
- 同一个 incident 在被拒绝一次后，仍可能继续调查、修改方案并再次发起审批
- 若直接把 incident 置为 `rejected` 或 `expired`，主流程会被过度切碎，恢复逻辑也会更复杂

因此当前推荐做法是：

- 审批拒绝或超时首先落在 approvals 记录中
- incident 依据策略继续保持 `pending_approval`、转为 `abnormal`，或回退到 `investigating`
- 所有这些变化都必须写入 timeline，避免只保留最终状态而丢失过程语义

### 25.3 delivery 补偿字段

消息投递不应混入 incident 主状态，而应作为独立补偿字段处理。建议字段包括：

- `delivery_status`
- `delivery_attempts`
- `last_delivery_error`
- `last_delivery_at`
- `target_type`
  例如 `status_card`、`thread_summary`、`approval_notice`
- `target_message_id`

使用原则：

- delivery 字段负责飞书界面最终一致，不负责主流程推进
- delivery 失败不会覆盖 incident 主状态，但会触发补偿恢复
- delivery 重试必须按目标对象幂等执行

### 25.4 remediation outcome 字段

执行后的结果建议作为 remediation outcome 或 timeline event 表达，而不是强行扩展 incident 主状态。建议表达以下结果：

- `success`
- `failed`
- `rollback_required`
- `rolled_back`

使用原则：

- 是否需要回滚，由执行结果和验证结果共同决定
- `rollback_required` 可以作为 remediation 的结果字段，也可以作为 timeline event
- 只有当回滚导致主流程进入人工处理时，incident 才转为 `abnormal`

### 25.4.1 为什么 `rollback_required` / `rolled_back` 不默认升格为 incident 主状态

审查意见中提出 `rollback_required`、`rolled_back` 是否需要单独主状态，这个提醒是有价值的，但当前建议仍保持克制：

- 回滚描述的是执行结果，不一定改变 incident 所处的主阶段
- 某些回滚是执行策略的一部分，回滚完成后 incident 仍可能继续 `verifying` 或回到 `investigating`
- 若把每种修复结果都提升为 incident 主状态，会让主状态机被执行细节污染

因此当前推荐做法是：

- `rollback_required` 和 `rolled_back` 默认作为 remediation outcome 字段或 timeline event
- 只有当回滚后需要人工接管、重新规划修复路径或系统出现不一致时，incident 才转 `abnormal`
- 回滚动作本身必须写 audit，并可单独参与回滚率统计

### 25.5 reopen 字段规范

为了稳定支持 reopen 语义，建议至少保留以下字段：

- `dedup_key`
- `dedup_key_version`
- `reopen_count`
- `resolved_at`
- `closed_at` 或等价归档时间字段

使用原则：

- `resolved_at` 用于判断是否仍在 reopen 窗口
- `closed_at` 用于判定是否已不可 reopen
- reopen 必须写入独立 timeline event，不能只改状态不留痕
- reopen 判定应同时参考 `dedup_key_version`，避免 dedup 语义调整后错误复用历史 incident

### 25.6 推荐字段归类原则

实现时建议遵循以下判定顺序：

1. 能否驱动主流程和恢复逻辑
2. 是否需要长期稳定展示给值班人员
3. 是否只影响审批、投递、回滚等局部子流程
4. 是否只应作为 timeline 事件保留

如果一个字段只影响局部子流程，不要优先把它升级为 incident 主状态。这样可以保持状态机稳定，避免实现复杂度失控。

## 26. 实施路线建议

### 26.0 当前项目实现差距与落地修订

MVP 已实现映射：

- `toolsets/incident_store.py`：incident 主状态、timeline、dedup、reopen、飞书绑定字段。
- `toolsets/approval_async.py`：审批状态、incident 关联、飞书审批消息关联。
- `toolsets/message_delivery.py`：主群卡片、Thread 摘要、审批通知的投递补偿状态。
- `hooks/alert_webhook.py`：Alertmanager firing 告警到 incident/timeline 的入口编排。
- `toolsets/system_mode.py`：平台级 normal/degraded/read_only 运行模式。

本设计文档描述的是飞书落地的目标方案。结合当前项目代码，现阶段更准确的状态是：SRE 工具能力和 SQLite 基础存储已经具备，但飞书 Thread 编排、消息补偿、incident 级 dedup / reopen 仍需要补齐。

当前已有能力：

- `toolsets/incident_store.py` 已提供 incident 基础表、timeline 事件表、状态更新、负责人更新和活跃事件查询。
- `toolsets/approval_async.py` 已提供异步审批记录、审批决策、执行结果回写和 SQLite WAL 写入重试。
- `toolsets/operation_lock.py` 已提供资源级互斥锁，可作为写操作串行化基础。
- `toolsets/audit_log.py`、`toolsets/cost_guard.py`、`toolsets/notification_manager.py` 已分别覆盖审计、成本和通知防疲劳的基础能力。
- `hooks/alert_webhook.py` 已能接收 Alertmanager webhook、校验 HMAC、提取告警字段并调用内存 dedup。

必须补齐的落地缺口：

- 告警进入后需要真正创建或复用 incident，而不是只返回 triage prompt。
- `incidents` 需要补齐飞书绑定字段、dedup 字段、`closed_at` 和 reopen 计数字段。
- `approvals` 需要补齐 `incident_id` 与 `approval_message_id`，否则审批无法和 Thread 通知、恢复补偿稳定关联。
- 需要新增 `message_deliveries` 存储与重试逻辑，用于主群卡片、Thread 摘要、审批通知的幂等投递。
- 需要把当前内存 dedup 升级为 incident 级 dedup：先查 open/resolved incident，再决定更新计数、reopen 或新建 incident。
- 需要实现 incident 状态迁移校验，避免任意工具直接把状态改到不合法阶段。
- 需要定义 `system_mode` 的存储位置和读取方式，可先使用轻量 SQLite 表或配置文件，后续再接入健康检查。

第一阶段建议只交付最小闭环：

1. Alertmanager firing -> dedup -> create incident -> write `alert_fired`。
2. 飞书主群发送状态卡片，并把 `chat_id / root_message_id / status_card_message_id` 回写 incident。
3. 创建或关联 Thread，并把 `thread_id` 回写 incident。
4. Thread 内触发 triage，所有工具结果只写摘要 timeline。
5. 高危动作进入 approval，审批通过后执行并写 audit / timeline。
6. 验证恢复后更新 `resolved_at`，超过 reopen 窗口后写 `closed_at`。

第一阶段明确不做：

- 不做多实例 Agent 和 PostgreSQL 迁移。
- 不做完整工单、CMDB、值班平台集成。
- 不把原始大输出长期写入 SQLite。
- 不依赖飞书 Thread 全量历史恢复状态。
- 不在权限、审批和操作锁未串联前开放高危自动修复。

建议新增配置项：

- `notification.reopen_window_seconds`：控制 `resolved` 后允许 reopen 的时间窗口。
- `notification.storm_threshold_per_minute`：控制告警风暴保护阈值。
- `sre.dedup_key_version`：标记 dedup 规则版本，避免规则变更后错误复用历史 incident。
- `sre.raw_output_ttl_hours`：控制原始大输出临时缓存保留时间。
- `sre.system_mode_store`：声明 `system_mode` 使用 SQLite、配置文件或外部存储。

验收标准建议：

- 一条 firing 告警能创建 incident，并产生 `alert_fired` timeline。
- 同一 dedup key 的重复 firing 不创建新 incident，只更新已有事件或计数。
- `resolved` 窗口内重复 firing 能 reopen，`closed` 后重复 firing 创建新 incident。
- 主群卡片、Thread 摘要、审批通知的发送结果都能在 `message_deliveries` 中查询和重试。
- 审批记录能反查到 incident、Thread 和具体审批消息。
- Agent 重启后能从 SQLite 恢复 active incident、pending approval 和未完成 delivery。

### 26.1 第一阶段：最小可用闭环

目标：把单 incident 从告警到关闭的最短链路打通。

包含：

- 主群告警卡片
- Thread 建立与绑定 incident
- triage / investigate / remediate 基础流程
- 审批、审计、恢复落库
- 基础 runbook 和语音摘要

### 26.2 第二阶段：运营健壮性增强

包含：

- 通知防疲劳
- LLM 降级
- 成本守卫
- 自监控
- 指标周报
- 拒绝学习

### 26.3 第三阶段：知识闭环

包含：

- 从 incident timeline 自动提炼草稿 skill
- runbook 审核与上线
- 高频故障模式标准化
- 失败经验沉淀

### 26.4 第四阶段：平台化演进

包含：

- SQLite -> PostgreSQL
- 多实例 Agent
- 更完善的审批中心
- 与工单、CMDB、值班平台集成

## 27. 不建议当前做的事

当前阶段不建议：

- 让主群承载所有诊断细节
- 把 Thread 全量历史直接送给模型
- 一开始就引入 Kafka / Redis / 向量库 / 复杂工作流引擎
- 用 RAG 替代 incident 的事务状态
- 在没有完善权限和审批前开放自动执行高危命令

## 28. 最终推荐

如果目标是在飞书上真正落地一个可用、可控、可恢复的 AIOps 助手，那么当前最合理的技术路线不是追求复杂度，而是先把边界定义清楚：

- 飞书主群负责入口和播报
- 飞书 Thread 负责单事件协作
- SQLite 负责事实源和长期记忆
- Incident 状态机负责真实会话推进
- Agent 按需组装上下文，不直接依赖聊天历史

这条路线兼顾了产品体验、工程可控性和后续演进空间，适合作为飞书版 AIOps SRE Agent 的正式落地方案。
