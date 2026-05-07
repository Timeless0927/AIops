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

### 2.1 当前实现状态（2026-05）

当前仓库已经完成 Phase 3 的状态闭环，但尚未进入自动执行闭环。文档中凡涉及修复执行、rollback、审批卡片按钮的内容，除非明确标注为“后续阶段”，都不应被理解为当前可用能力。

已完成：

- Alertmanager webhook 可以创建或复用 incident，并写入 timeline。
- 诊断分析、evidence、case recall 和 Feishu thread 摘要具备持久化基础。
- `approval_async` 支持 request/check/resolve/expire，并能绑定 `incident_id`。
- `hooks/approval_reply.py` 支持 `批准 <approval_id>` / `拒绝 <approval_id> <reason>`。
- 父项目 `runtime.feishu_approval_overlay` 在真实 Hermes Feishu gateway 入站路径中拦截文本审批回复。
- `voice_context.py` 只做 incident 上下文增强，不承载审批状态变更。

未完成：

- 飞书交互卡片按钮。
- 审批人 RBAC 授权。
- 审批通过后的真实 `k8s_write` / `k8s_exec` 执行。
- dry-run、operation lock、执行后健康检查、rollback 的完整执行链路。

当前审批数据流：

```text
Alertmanager firing
  |
  v
alert_webhook -> incident + analysis + pending approval
  |
  v
Feishu thread summary shows approval_id
  |
  v
User replies: 批准 <id> / 拒绝 <id> <reason>
  |
  v
runtime overlay -> hooks.approval_reply -> approval_async.resolve_approval
  |
  v
incident timeline records approval_approved / approval_denied
```

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
- 已形成建议动作或修复意图，但等待人工审批。当前实现只更新审批状态，不执行修复。

`executing`
- 后续自动执行阶段使用：审批、dry-run、锁和审计均通过后，正在执行修复动作。当前实现不会自动进入该状态。

`verifying`
- 后续自动执行阶段使用：修复动作已完成，正在核验恢复效果。当前实现不会自动进入该状态。

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

后续自动执行无需审批时（当前尚未开放）：

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

### 13.1 当前审批原则

当前实现只完成“审批状态闭环”，不执行修复。所有写操作和 exec 操作都不能只靠 LLM 决定，后续进入执行闭环前必须先经过权限规则、审批规则、dry-run、锁和审计。

当前已经固定的原则：

1. 审批状态变更只有一个入口：`hooks/approval_reply.py`。
2. Feishu gateway overlay 只做路由和回复，不直接写业务状态。
3. `voice_context.py` 只做上下文增强，不处理审批。
4. 缺少审批人身份时必须拒绝处理，不修改 approval。
5. 审批通过当前只表示 approval 进入 `approved`，不代表执行已经发生。

### 13.2 当前 Feishu 文本审批协议

当前 MVP 只支持精确文本命令：

- `批准 <approval_id>`
- `拒绝 <approval_id> <reason>`

处理结果需要回到同一飞书 chat/thread：

- 成功批准：`审批已批准：<approval_id>`
- 成功拒绝：`审批已拒绝：<approval_id>`
- 处理失败：`审批处理失败：<reason>`

失败场景必须安全：

- 未知 approval ID：返回清晰错误，不写 incident timeline。
- 缺少 sender open_id/user_id：返回“无法识别审批人身份”，不调用审批处理器。
- 非审批文本：进入 Hermes 原始消息流。

### 13.3 后续审批授权

下一阶段应在 `approval_reply` 或其下游授权层加入审批人授权，而不是只检查身份存在。建议顺序：

1. 从 Feishu `open_id` 映射 operator profile。
2. 校验 operator 是否 `can_approve`。
3. 校验 operator 是否允许审批该 namespace / risk_level / operation_type。
4. 拒绝未授权审批，并写审计或 timeline 事件。
5. 授权通过后再调用 `approval_async.resolve_approval()`。

### 13.4 后续修复执行策略

自动执行必须作为独立阶段实现，不应塞进 Feishu reply handler。推荐新增 execution coordinator：

```text
approval approved
  |
  v
execution coordinator
  |- re-check authorization
  |- server-side dry-run or explicit fallback
  |- acquire operation lock
  |- execute minimal allowed action
  |- write audit + timeline
  |- run health check
  `- rollback or escalate on failure
```

执行闭环的最小约束：

1. 只支持 allowlist 中的低风险 `k8s_write` 作为第一批。
2. `k8s_exec` 不进入自动执行，除非后续单独设计。
3. 每个 execution 必须有幂等 key，防止同一 approval 重复执行。
4. 审批通过但抢锁失败时，不执行修复，必须在 Thread 中明确提示。
5. dry-run 失败时不进入真实执行。

### 13.5 审批授权实现规格

完整实现级规格见 `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md`。本节保留核心设计约束，避免主设计和开发规格分裂。

后续审批授权必须在 approval resolve 前完成，且失败时不得修改 approval 状态。

建议新增 `hooks/approval_authorization.py`，提供：

```python
authorize_approval_reply(
    *,
    approval: dict[str, Any],
    approver_id: str,
    decision: str,
) -> dict[str, Any]
```

输入 approval 来自 `approval_async.check_approval()`，必须包含：

- `approval_id` / `id`
- `status`
- `operation_type`
- `namespace`
- `risk_level`
- `requester`
- `context`
- `incident_id`

授权规则按顺序执行：

1. approver_id 必须存在。
2. approver_id 必须映射到 `sre_permissions.operators` 中的 operator。
3. operator 必须 `can_approve=true`，或匹配审批规则允许的 role/name。
4. operator namespace 范围必须覆盖 approval namespace，`*` 表示全局。
5. `k8s_exec` 和 dangerous 操作必须 admin / can_approve 用户审批。
6. requester 默认不能审批自己的高风险操作。
7. approval 必须仍是 `pending`。

安全默认配置：

```yaml
sre_permissions:
  approval_policy:
    allow_self_approval_low_risk: false
    require_admin_for_exec: true
    require_admin_for_dangerous: true
```

授权失败 reason_code：

- `missing_approver_id`
- `unknown_approver`
- `approver_not_allowed`
- `namespace_not_allowed`
- `self_approval_denied`
- `approval_not_pending`

未授权尝试必须留痕：有 `incident_id` 时写 `approval_unauthorized` timeline；没有 incident 时写 audit 或 warning log。

### 13.6 自动执行设计规格

自动执行必须由独立 execution coordinator 推进，不允许在 `hooks/approval_reply.py` 内直接执行。

建议新增 `toolsets/approval_execution.py`：

```text
approved approval
  |
  v
approval_execution.process_pending_executions(limit=N)
  |- find approved but not executed approvals
  |- create execution record / idempotency key
  `- process one execution at a time
```

建议新增 `approval_executions` 表：

```sql
CREATE TABLE approval_executions (
  id TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL UNIQUE,
  incident_id TEXT,
  action_signature TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL,
  dry_run_result_json TEXT,
  lock_key TEXT,
  audit_id INTEGER,
  health_result_json TEXT,
  rollback_result_json TEXT,
  error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  completed_at REAL
);
```

execution 子状态：

- `queued`
- `dry_run_running`
- `dry_run_failed`
- `lock_waiting`
- `executing`
- `health_checking`
- `succeeded`
- `failed`
- `rollback_required`
- `rolled_back`
- `cancelled`

第一版只接受结构化 action，不接受自然语言或自由 kubectl：

```json
{
  "action_type": "scale_deployment",
  "cluster": "prod-a",
  "namespace": "default",
  "resource_kind": "deployment",
  "resource_name": "nginx",
  "parameters": {"replicas": 3}
}
```

第一版 allowlist：

- `scale_deployment`
- `restart_deployment`，只能通过受控 rollout restart 实现

第一版明确不支持：

- `k8s_exec`
- `kubectl delete`
- namespace / node / PV / CRD 操作
- 自由 shell 命令
- 多资源批量写

执行流：

```text
process approved approval
  |
  |- load approval + incident
  |- verify approval.status == approved
  |- verify execution does not already exist
  |- normalize context into allowlisted action
  |- re-run authorization snapshot checks
  |- create approval_execution(status=queued)
  |- run dry-run
  |    |- failed -> dry_run_failed, timeline, notify, stop
  |- acquire operation_lock(lock_key)
  |    |- failed -> lock_waiting/failed, timeline, notify, stop
  |- update incident status executing
  |- execute action through safe tool API
  |- write audit_log
  |- run health check
  |    |- healthy -> succeeded, timeline, notify
  |    `- unhealthy -> rollback_required, timeline, notify human
  `- release lock
```

Dry-run 策略：

- 优先 server-side dry-run。
- client-side dry-run 只能在低风险操作且配置允许时使用。
- 无法 dry-run 时不得自动执行。
- dry-run 结果必须保存到 execution record。

Lock 策略：

- lock key：`{cluster}:{namespace}:{resource_kind}/{resource_name}`。
- dry-run 后、真实执行前获取锁。
- 锁冲突阻止执行，但不回滚 approval 状态。
- 必须在 finally 中释放锁，进程崩溃依赖 TTL 清理。

健康检查策略：

- Deployment desired/available replicas 符合预期。
- 相关 Pod 在 timeout 内 Ready。
- 观察窗口内 restart count 不继续增长。
- 近期 events 不出现新的 Failed / BackOff / OOMKilled。

Rollback 策略：

第一版不自动 rollback。健康检查失败时标记 `rollback_required` 并通知人工。只有具备确定逆操作和快照恢复能力的 action，后续才允许自动 rollback。

用户可见消息：

- dry-run failed：`审批已批准，但 dry-run 失败，未执行：<reason>`
- lock conflict：`审批已批准，但资源正在被其他操作占用，未执行：<lock_key>`
- success：`审批动作已执行并通过健康检查：<approval_id>`
- health failed：`审批动作已执行，但健康检查失败，需要人工确认：<approval_id>`
- duplicate：`审批动作已处理过，未重复执行：<approval_id>`

### 13.7 开发测试矩阵

审批授权测试：

- Admin 可以批准 pending approval。
- Admin 可以拒绝，并保留完整 reason。
- 未知 Feishu 用户被拒，且不调用 `resolve_approval()`。
- 非 admin 不能批准 high-risk/dangerous 操作。
- namespace 受限审批人不能审批其他 namespace。
- 高风险自批默认拒绝。
- 已 resolved / expired approval 返回清晰错误，不重复写成功 timeline。

自动执行测试：

- approved approval 创建一条 execution record。
- coordinator 跑两次不会重复执行。
- 非 approved approval 被忽略。
- free-form / unknown action 被拒绝。
- dry-run 失败阻止执行。
- lock conflict 阻止执行并通知。
- 成功执行写 audit 和 timeline。
- 健康检查失败标记 `rollback_required`，不声明 resolved。
- approval context 必须包含结构化 action 和 `action_signature`。

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
- `回滚率`：后续自动执行阶段指标，按 audit 记录粒度统计 `rollback=1 / 全部修复相关审计记录`
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
建议动作：kubectl scale deployment/payments-api --replicas=4 -n payments
风险等级：standard
当前状态：等待审批
审批方式：回复 `批准 <approval_id>` 或 `拒绝 <approval_id> <reason>`
注意：当前审批通过只更新状态，不自动执行该命令
```

### 21.5 后续修复完成消息（自动执行阶段）

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

当前时序如下：

1. incident 状态进入 `investigating`。
2. 记录 `investigate_start`。
3. 汇总日志、指标、事件和配置证据。
4. 输出根因候选、下一步建议和风险说明。
5. 记录 `investigate_end`。
6. 从 `next_best_actions` 中选择一个 approval candidate。
7. 创建或复用 pending approval，并关联 incident/action signature。
8. 在 timeline 中记录 `approval_requested` 或 `approval_skipped`。
9. 在 Thread 摘要中展示 approval ID 和文本审批指令。

关键约束：

- 审批是对修复意图的授权，不等于已经取得执行资格。
- 审批记录必须与 incident、namespace、risk_level、operation_type 和 context 建立可追溯关联。
- 同一 incident/action signature 不应重复创建 pending approval。

### 24.4 Approval Reply 到 Approval State

当前时序如下：

1. 用户在同一 Feishu chat/thread 回复 `批准 <id>` 或 `拒绝 <id> <reason>`。
2. runtime overlay 在 Hermes LLM/session 流程前识别审批命令。
3. overlay 提取 sender `open_id`，缺失时才回退 `user_id`。
4. 缺少身份时直接回复失败，不调用审批处理器。
5. `hooks/approval_reply.py` 解析命令并调用 `approval_async.resolve_approval()`。
6. 成功后写入 `approval_approved` 或 `approval_denied` timeline。
7. overlay 将处理结果回复到同一 chat/thread。
8. 已处理审批文本停止在 overlay，不进入 LLM。

关键约束：

- 当前不进入 `executing`。
- 当前不获取 operation lock。
- 当前不调用 `k8s_write` / `k8s_exec`。
- 后续自动执行必须由 execution coordinator 接管，而不是复用 reply handler 直接执行。

### 24.4.1 后续 Approval 到 Execute（尚未实现）

后续执行闭环建议时序：

1. execution coordinator 发现 approved approval。
2. 重新校验审批人授权和操作风险等级。
3. 执行 server-side dry-run 或明确记录降级原因。
4. 获取 operation lock。
5. 获取锁成功后，incident 才能进入 `executing`。
6. 执行最小必要修复动作。
7. 写入 audit 和 timeline。
8. 执行后健康检查。
9. 成功进入 `verifying`，失败进入 rollback 或人工介入路径。

关键约束：

- 审批通过但抢锁失败时，不得执行修复。
- 锁冲突必须对用户明确可见，不能静默吞掉。
- 高危操作的审批级别必须在执行前再次校验，不能只依赖历史上下文。

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

### 26.0 当前项目实现状态与落地修订

当前实现已经超过早期 MVP 计划中的“只返回 triage prompt”阶段。最新事实源如下：

已完成：

- `hooks/alert_webhook.py`：Alertmanager firing 告警入口、HMAC 校验、dedup、incident 创建/复用、targeted read-only evidence、analysis 持久化、case recall、Feishu thread summary、Phase 3 approval request。
- `toolsets/incident_store.py`：incident 主状态、timeline、analysis、evidence、case profile、Feishu 绑定字段、dedup/reopen 支撑。
- `toolsets/approval_async.py`：approval request/check/resolve/expire、incident 关联、pending approval 复用、SQLite WAL 写入重试。
- `hooks/approval_reply.py`：飞书文本审批解析和 approval resolve，成功后回写 incident timeline。
- `runtime/feishu_approval_overlay.py`：真实 Hermes Feishu gateway 入站路径拦截审批文本。
- `runtime/hermes_gateway.py`：先安装 overlay，再启动 Hermes gateway runner。
- `hooks/voice_context.py`：飞书 thread / 语音场景 incident context enrichment。
- `toolsets/sre_metrics.py`：pending approval 和 approval backlog 指标。

当前边界：

- 不修改 `hermes-agent` 上游代码。
- 不维护 Hermes 业务 fork。
- 不支持飞书审批卡片按钮。
- 不做审批通过后的真实 Kubernetes 写操作。
- 不做 dry-run、operation lock、健康检查和 rollback 执行链路。

后续优先级：

1. **Overlay 生产化收口**：补齐运行文档、升级检查、失败回退和 smoke test 说明。
2. **审批人授权**：基于 Feishu open_id 映射 operator，拒绝未授权审批人。
3. **自动执行设计**：先设计 execution coordinator、状态机、dry-run、锁、审计、健康检查和 rollback。
4. **自动执行实现**：只在设计确认后开放低风险 allowlist 的 `k8s_write`，`k8s_exec` 单独设计。

验收标准：

- `批准 <id>` 能更新 approval 并写 timeline。
- `拒绝 <id> <reason>` 能保留完整拒绝原因。
- 未知 approval ID 返回清晰错误。
- 缺少 sender 身份时不调用审批处理器。
- 普通 Feishu 文本仍进入 Hermes 原始消息流。
- `hermes-agent` 子模块不出现业务代码改动。

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
