# 变更请求

本文件是三个角色之间的共享通信总线。

## 状态值

- `draft`
- `needs-product-review`
- `needs-architecture-review`
- `approved`
- `in-progress`
- `done`
- `rejected`
- `deferred`

## 入口规则

`dev-lead-agent` 必须在修改代码前，把所有会改变行为的用户反馈登记到这里。

## 模板

```markdown
## CR-YYYY-MM-DD-NNN：简短标题

状态：draft
负责人：dev-lead-agent
创建日期：YYYY-MM-DD

### 用户反馈

待补充。

### 初筛影响

- PDD：无 / 可能 / 必须
- BDD：无 / 可能 / 必须
- DDD：无 / 可能 / 必须
- SDD：无 / 可能 / 必须
- TDD：无 / 可能 / 必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要 / 需要 / 已批准 / 已拒绝
- architect-agent：不需要 / 需要 / 已批准 / 已拒绝

### 决策

待补充。

### 影响文件

- 待补充。

### 验收标准

- [ ] 待补充。

### 验证

待补充。
```

## 待处理请求

## CR-2026-05-09-001：容器部署补齐 Feishu 群消息策略

状态：done
负责人：dev-lead-agent
创建日期：2026-05-09

### 用户反馈

本地启动 Hermes 后在飞书群里 @ 机器人可以响应；Docker 打包部署后不响应，怀疑配置没有传入容器。

### 初筛影响

- PDD：无
- BDD：无
- DDD：无
- SDD：无
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要
- architect-agent：不需要

### 决策

这是部署配置传播缺口：Hermes Feishu adapter 默认 `FEISHU_GROUP_POLICY=allowlist` 且 `FEISHU_ALLOWED_USERS` 为空，会丢弃群消息。容器入口和 K8s ConfigMap 默认设置 `FEISHU_GROUP_POLICY=open`，保持与本地可 @ 响应行为一致；生产可改回 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。

### 影响文件

- `deploy/entrypoint.sh`
- `deploy/hermes-config.template.yaml`
- `deploy/k8s/configmap.yaml`
- `docs/user-guide.md`
- `docs/development-progress.md`
- `docs/TODD.md`
- `tests/test_deploy_entrypoint.py`
- `tests/test_k8s_manifests.py`

### 验收标准

- [x] 容器入口默认导出 `FEISHU_GROUP_POLICY=open`。
- [x] K8s ConfigMap 显式包含 `FEISHU_GROUP_POLICY` 和 `FEISHU_ALLOWED_USERS`。
- [x] 渲染后的 Hermes runtime config 明确记录群消息默认策略。
- [x] focused 部署测试通过。

### 验证

2026-05-09：`rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q`，9 passed。

## CR-2026-05-11-001：将 Hermes 核心状态迁移到 PVC

状态：approved-for-implementation
负责人：dev-lead-agent
创建日期：2026-05-11

### 用户反馈

镜像部署后 Feishu pairing、skills 安装、memory 和记忆状态在 pod 重启后丢失；希望把需要持久化的 Hermes 状态统一落到 PVC。

### 初筛影响

- PDD：无
- BDD：无
- DDD：无
- SDD：必须
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要
- architect-agent：已批准

### 决策

采用单个 PVC 挂载 `/data`，将 AIOps SQLite 统一放到 `/data/aiops`，将 Hermes 核心状态放到 `/data/hermes`，并把默认 `HERMES_HOME` / `HERMES_CONFIG` / `AIOPS_DATA_DIR` 切到这两个目录。这样 pairing、auth、session、skills、memory、AIOps 本地状态都会随 pod 重启保留。

### 影响文件

- `Dockerfile.aiops`
- `deploy/entrypoint.sh`
- `deploy/k8s/configmap.yaml`
- `deploy/k8s/README.md`
- `docs/user-guide.md`
- `docs/development-progress.md`
- `docs/TODD.md`
- `tests/test_data_dir_env.py`
- `tests/test_deploy_entrypoint.py`
- `tests/test_k8s_manifests.py`
- `toolsets/cost_guard.py`
- `toolsets/rejection_learner.py`

### 验收标准

- [x] Dockerfile 与 K8s ConfigMap 的默认路径都指向 `/data/hermes` 和 `/data/aiops`。
- [x] Hermes pairing、auth、session、skills、memory 路径随 `HERMES_HOME` 落到 PVC。
- [x] `cost_tracking.db` 和 `rejection_lessons.json` 也跟随 `AIOPS_DATA_DIR` 落到 PVC。
- [x] 相关部署测试和数据目录测试通过。

### 验证

2026-05-11：`pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q`，21 passed。

## CR-2026-05-11-002：审批卡片强制投递与回写

状态：done
负责人：dev-lead-agent
创建日期：2026-05-11

### 用户反馈

审批流程现在只会创建 approval record，飞书审批卡片可能没发出去，导致 `status=pending` 但 `approval_message_id=null`。需要把“创建审批记录”“发送飞书审批卡片”“回写 approval_message_id”绑成一个可见性闭环，并且能补发历史坏状态。

### 初筛影响

- PDD：可能
- BDD：可能
- DDD：可能
- SDD：必须
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：已批准
- architect-agent：已批准

### 架构影响

已更新 `docs/03-SDD.md`，把审批创建成功定义为 `approval record` 创建、飞书审批卡片发送成功、`approval_message_id` 回写完成的可见性闭环。Alertmanager 入口必须先发送 incident 状态消息并完成飞书绑定回写，再创建或补发审批卡片。

`message_delivery` outbox / recovery 补偿承担最终一致投递，不是原子事务。非原子窗口中可能出现飞书发送成功但本地回写失败；实现必须使用稳定 `uuid` / outbox 幂等键和 recovery 补发、补回写来消解重复投递和坏状态。缺少飞书绑定目标时只能返回 `pending_retry` 或 `failed`，不得误报审批卡片已可见。

### 决策

审批可见性的判定标准从“只创建 approval record”升级为“approval record created + Feishu approval card sent + approval_message_id written back”。Alertmanager 主流程必须先完成 incident 的飞书绑定，再发审批卡片；已有 `pending` 且 `approval_message_id` 为空的记录要能自动或手动补发。

### 产品/领域评审

product-domain-agent：已批准。该 CR 不扩大产品目标，但改变人工审批的可验收行为和领域语义：有效可见审批不再只是 `status=pending`，还必须满足飞书审批卡片已投递且 `approval_message_id` 已回写；`pending + approval_message_id=null` 是待补偿异常状态，应补发审批卡片而不是静默等待；投递失败时不得让 SRE 或调用方误认为审批已经可见或可操作。

### 影响文件

- `hooks/alert_webhook.py`
- `hooks/feishu_conversation.py`
- `hooks/recovery.py`
- `runtime/feishu_approval_overlay.py`
- `toolsets/approval_async.py`
- `toolsets/message_delivery.py`
- `tests/test_alert_webhook.py`
- `tests/test_approval_async.py`
- `tests/test_feishu_conversation.py`
- `tests/test_feishu_approval_overlay.py`
- `tests/test_recovery.py`
- `tests/test_message_delivery.py`
- `docs/01-BDD.md`
- `docs/02-DDD.md`
- `docs/03-SDD.md`
- `docs/04-IMPLEMENTATION-PLAN.md`
- `docs/05-TDD-TEST-PLAN.md`
- `docs/development-progress.md`
- `docs/TODD.md`

### 验收标准

- [x] `sre_request_approval(...)` 返回必须包含 `ok=true`、`approval_id`、非空 `approval_message_id` 和 `delivery_status=sent`。
- [x] 飞书群或线程审批卡片发送调用和按钮回调 payload 已被本地自动化覆盖；真实飞书群/线程端到端验收转入剩余风险。
- [x] 已有 `status=pending` 且 `approval_message_id=null` 的审批能被补发，补发后 `approval_message_id != null`。
- [x] Alertmanager 主流程先发布 incident 状态消息并回写绑定，再创建/补发审批卡片。
- [x] 启动恢复路径会扫描旧的 pending 审批并尝试补发卡片。

### 验证

- 2026-05-11：implementation-agent 完成审批卡片投递、`approval_message_id` 回写、旧 pending 补发、startup recovery 和 sent outbox 补回写实现。
- 2026-05-11：test-agent 运行 `python3 -m py_compile toolsets/approval_async.py toolsets/message_delivery.py hooks/feishu_conversation.py hooks/alert_webhook.py hooks/recovery.py runtime/feishu_approval_overlay.py`，通过。
- 2026-05-11：test-agent 运行 `python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q`，`68 passed, 14 warnings`。
- 2026-05-11：review-agent 最终复审无 Critical/Important 代码阻塞；真实飞书群/线程端到端验收仍待执行。
- 2026-05-18：最终复验通过：`tests/test_alert_webhook_server.py tests/test_data_dir_env.py` 3 passed；`tests/test_approval_async.py` 27 passed；主目标 focused tests 95 passed, 15 warnings；`config.yaml` 与 `deploy/hermes-config.template.yaml` YAML 解析通过。
- 2026-05-18：review-agent 复审无阻断；确认 `toolsets` import shadowing 修复未落到 Hermes 同名模块。

## CR-2026-05-15-001：接入飞书原生审批

状态：in-progress
负责人：dev-lead-agent
创建日期：2026-05-15

### 用户反馈

将审批人权限、审批链、审批记录切到飞书原生审批；AIOps 保留本地审批状态机、执行安全、operation lock、审计、健康检查和回滚。当前自定义卡片审批降级为通知展示/回退方案，不再作为主审批入口。

目标流程：Alertmanager 触发后由 AIOps 分析告警并创建本地 approval，再创建飞书原生审批实例；人类在飞书审批中心审批后，飞书审批事件 webhook 秒级回调并同步本地状态；只有本地 approval 进入 `approved` 后，execution worker 才能执行。

补充排查：当前 Pod 里的 `HERMES_CONFIG=/data/hermes/config.yaml` 已正确注入，但 `hooks/alert_webhook.py` 与 `toolsets/approval_async.py` 仍需要优先尊重运行时配置路径。若它们继续硬编码读取仓库根 `config.yaml`，就会把 `approval.enabled=true` 的 runtime 配置误判成 `false`，从而回退到旧卡片审批。

### 初筛影响

- PDD：必须
- BDD：必须
- DDD：必须
- SDD：必须
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：已批准。已更新 PDD/BDD/DDD，并复查 DDD 主语，确认有效待审批 = 本地 `external_pending` + 飞书原生审批实例已创建并可在审批中心处理。
- architect-agent：已批准。已更新 SDD 和 ADR，确认生产路径不依赖 `lark-cli`，外部事件只同步本地状态，不直接执行。
- implementation-agent：已完成本地实现。
- test-agent：已完成 Red-Green 回归和本地验证。
- review-agent：已批准。两轮阻断项已修复，最终复审 `APPROVED`。

### 架构影响

已由 architect-agent 评审并更新 `docs/03-SDD.md` 与 `docs/adr/0001-feishu-native-approval.md`。架构影响包括：

- 新增 `hooks/feishu_approval_event.py` 接收飞书“审批实例状态变更”事件，只做状态同步，不直接执行命令。
- 新增 `toolsets/feishu_native_approval.py` 调用飞书 OpenAPI `POST /approval/v4/instances` 创建审批实例，不使用 `lark-cli` 作为生产依赖。
- `toolsets/approval_async.py` 增加外部审批字段、`external_pending` 状态和 `resolve_external_approval(...)` 幂等同步入口。
- `hooks/alert_webhook.py` 从主路径“发送可点击审批卡片”改为“创建飞书原生审批 + 线程通知审批链接和摘要”。
- `hooks/feishu_conversation.py` 保留 incident thread 回写，自定义审批卡片降级为可选通知/回退。
- `hooks/recovery.py` 或等价 worker 增加 external_pending polling 补偿，防止飞书事件丢失。
- `hooks/alert_webhook_server.py` 在实际 webhook 服务入口注册 `/webhooks/feishu/approval`，避免飞书事件只靠 polling 补偿。

### 决策

- 飞书只决定“人类是否批准”，AIOps 仍是执行权威。
- 飞书审批实例 `uuid` 使用本地 `approval_id`，`instance_code` 写回本地 `external_instance_code`。
- 飞书状态映射：`APPROVED -> approved`，`REJECTED -> denied`，`CANCELED -> canceled`。
- 已执行或终态 approval 不能被重复事件、延迟事件或 polling 结果回滚。
- 原自定义审批卡片不再作为主审批入口，只保留通知展示/回退能力。
- `card_fallback.enabled` 默认只做通知/回退展示；若未来允许旧卡片作为可批准 fallback，必须另开 CR 定义与原生审批互斥规则和验收口径。

### 产品/领域评审

product-domain-agent：已批准。已更新 `docs/00-PDD.md`、`docs/01-BDD.md`、`docs/02-DDD.md`；随后根据架构反馈复查 `docs/02-DDD.md`，确认“飞书审批卡片可见性”只描述旧通知/回退，不再作为主审批事实。

### 影响文件

- `hooks/feishu_approval_event.py`
- `hooks/alert_webhook.py`
- `hooks/feishu_conversation.py`
- `hooks/recovery.py`
- `hooks/alert_webhook_server.py`
- `toolsets/feishu_native_approval.py`
- `toolsets/approval_async.py`
- `runtime/feishu_approval_overlay.py`
- `tests/test_feishu_native_approval.py`
- `tests/test_feishu_approval_event.py`
- `tests/test_approval_async.py`
- `tests/test_alert_webhook.py`
- `tests/test_alert_webhook_server.py`
- `tests/test_recovery.py`
- `tests/test_feishu_approval_config.py`
- `docs/00-PDD.md`
- `docs/01-BDD.md`
- `docs/02-DDD.md`
- `docs/03-SDD.md`
- `docs/adr/0001-feishu-native-approval.md`
- `docs/04-IMPLEMENTATION-PLAN.md`
- `docs/05-TDD-TEST-PLAN.md`
- `docs/development-progress.md`
- `docs/TODD.md`

### 验收标准

- [x] 需要审批的告警修复动作会创建本地 approval，并成功调用飞书原生审批创建实例。（本地自动化覆盖）
- [x] 本地 approval 保存 `external_provider`、`external_instance_code`、`external_uuid`、`external_status`、`external_created_at`、`external_updated_at`。
- [x] 飞书审批事件 webhook 能校验事件来源，按 `uuid` / `instance_code` 找到本地 approval，并幂等同步 `APPROVED` / `REJECTED` / `CANCELED`。
- [x] `resolve_external_approval(...)` 不允许已 `executed`、`failed`、`denied`、`canceled`、`expired`、`approved`、`approval_create_failed` 的 approval 被外部事件回滚或误批准。
- [x] 只有本地 `approved` 状态能进入 execution worker；`external_pending`、`approval_create_failed`、未知外部事件都不能执行。
- [x] 飞书原生审批创建失败时，本地状态进入 `approval_create_failed`，并且不会执行动作。
- [x] incident thread 回写审批已发起、审批链接、风险摘要和操作摘要；自定义审批卡片不是主审批入口。
- [x] polling 补偿 worker 能同步遗失 webhook 的 `external_pending` 审批，带 batch size、间隔和失败退避限制。
- [x] 配置支持 `platforms.feishu.approval.enabled`、`approval_code`、`user_id_type`、`callback_path`、`polling_enabled`、`polling_interval_seconds`、`polling_batch_size`。
- [ ] 使用真实 `FEISHU_APPROVAL_CODE`、真实飞书审批中心和真实事件订阅做端到端人工/集成环境验收。

### 验证

- 2026-05-15：product-domain-agent 已批准并更新 PDD/BDD/DDD；随后复查 DDD，确认自定义审批卡片不再作为主审批事实。
- 2026-05-15：architect-agent 已批准并更新 SDD 与 `docs/adr/0001-feishu-native-approval.md`。
- 2026-05-15：test-agent 先补 Red 测试，初始 focused 结果 `30 failed, 42 passed, 14 warnings`。
- 2026-05-15：implementation-agent 完成原生审批客户端、事件 webhook、approval 外部状态机、alert webhook 主路径和 polling 补偿，focused 结果 `72 passed, 14 warnings`。
- 2026-05-15：review-agent 首轮审查阻断，随后补状态机终态、route、安全校验、polling、fallback、配置回归；最终复审 `APPROVED`。
- 2026-05-15：`python3 -m py_compile toolsets/approval_async.py toolsets/feishu_native_approval.py hooks/feishu_approval_event.py hooks/alert_webhook.py hooks/feishu_conversation.py hooks/recovery.py runtime/feishu_approval_overlay.py hooks/alert_webhook_server.py`，通过。
- 2026-05-15：`python3 -m pytest tests/test_alert_webhook_server.py tests/test_feishu_approval_config.py tests/test_feishu_approval_event.py -q`，`17 passed, 1 warning`。
- 2026-05-15：`python3 -m pytest tests/test_feishu_native_approval.py tests/test_feishu_approval_event.py tests/test_approval_async.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_feishu_conversation.py tests/test_feishu_approval_config.py tests/test_alert_webhook_server.py -q`，`93 passed, 15 warnings`。
- 2026-05-15：`python3 -m pytest tests/ -q`，`317 passed, 15 warnings`。
- 2026-05-18：review-agent 发现普通本地 `pending` 可被外部飞书事件误推进为 `approved`；test-agent 补红灯 `test_resolve_external_approval_ignores_local_pending_without_external_binding`，implementation-agent 修复 `resolve_external_approval(...)` 外部绑定边界，复验 `tests/test_approval_async.py` 27 passed。
- 2026-05-18：修复 Hermes registry 导入导致的 `toolsets` import shadowing；组合复验 `tests/test_alert_webhook_server.py tests/test_data_dir_env.py` 3 passed。
- 2026-05-18：最终 focused tests 95 passed, 15 warnings；`config.yaml` 与 `deploy/hermes-config.template.yaml` YAML 解析通过；review-agent 复审无阻断。
- 真实飞书审批中心端到端、真实事件订阅和真实 polling 补偿仍待人工/集成环境验收。
- 2026-05-19：现场排查确认 `alert_webhook` / `approval_async` 还需补齐 `HERMES_CONFIG`/`HERMES_HOME` 配置路径解析；当前 `/app/config.yaml` 与 `/data/hermes/config.yaml` 不一致会把 native approval 错误降级为旧卡片。

## CR-2026-05-19-001：统一 Hermes 运行时配置文件

状态：已完成

### 用户反馈

现场排查确认：Pod 中 `HERMES_CONFIG=/data/hermes/config.yaml` 已开启飞书原生审批，但 `hooks.alert_webhook_server` 仍会走旧自定义卡片审批。原因是 `hooks/alert_webhook.py` 与 `toolsets/approval_async.py` 读取了仓库根 `config.yaml`，没有统一使用 runtime config。

### 初筛影响

- PDD：无影响，审批目标不变。
- BDD：新增一条运行时配置单一来源回归。
- DDD：无影响。
- SDD：有影响，限定于配置解析路径和运行时默认值。
- TDD：必须补回归测试。
- TODD：必须同步当前任务、进展和验证记录。

### 必要评审

- product-domain-agent：不需要。
- architect-agent：已快速评审。结论：统一为 `HERMES_CONFIG -> HERMES_HOME/config.yaml`，允许删除仓库根 `config.yaml`。
- implementation-agent：需要。
- test-agent：需要。
- review-agent：需要。

### 决策

- `HERMES_CONFIG` 为最高优先级。
- 无 `HERMES_CONFIG` 时读取 `HERMES_HOME/config.yaml`。
- 仓库根 `config.yaml` 不再作为运行时配置来源。
- 删除仓库根 `config.yaml`，避免镜像或本地路径里出现第二份容易误读的配置文件。

### 影响文件

- `hooks/alert_webhook.py`
- `hooks/identity.py`
- `runtime/feishu_approval_overlay.py`
- `toolsets/approval_async.py`
- `toolsets/alert_dedup.py`
- `toolsets/cost_guard.py`
- `toolsets/llm_fallback.py`
- `toolsets/notification_manager.py`
- `toolsets/query_guard.py`
- `toolsets/skill_promotion.py`
- `config.yaml`
- `tests/test_alert_webhook.py`
- `tests/test_approval_async.py`
- `tests/test_approval_reply.py`
- `tests/test_feishu_approval_config.py`
- `tests/test_feishu_approval_overlay.py`
- `tests/test_hermes_entry.py`
- `tests/test_identity_extended.py`
- `tests/test_permission_guard.py`
- `docs/TODD.md`
- `docs/development-progress.md`

### 验收标准

- [x] `HERMES_CONFIG=/tmp/config.yaml` 时，相关运行时模块读取该文件。
- [x] 仅设置 `HERMES_HOME=/tmp/hermes` 时，相关运行时模块读取 `/tmp/hermes/config.yaml`。
- [x] `platforms.feishu.approval.enabled=true` 时，`hooks.alert_webhook` 进入 native approval 分支。
- [x] 仓库根 `config.yaml` 被删除，且核心运行时代码不再依赖 repo-root fallback。

### 验证

- 2026-05-19：architect-agent 快速评审通过，确认删除 repo-root `config.yaml` 与统一 `HERMES_CONFIG -> HERMES_HOME/config.yaml` 符合架构方向。
- 2026-05-19：implementation-agent 统一 `hooks/alert_webhook.py`、`hooks/identity.py`、`toolsets/approval_async.py`、`toolsets/alert_dedup.py`、`toolsets/cost_guard.py`、`toolsets/llm_fallback.py`、`toolsets/notification_manager.py`、`toolsets/query_guard.py`、`toolsets/skill_promotion.py` 的配置来源，并删除仓库根 `config.yaml`。
- 2026-05-19：test-agent 修正测试不再依赖仓库根 `config.yaml`，focused 命令 `rtk python3 -m pytest tests/test_alert_webhook.py tests/test_approval_async.py tests/test_approval_reply.py tests/test_identity_extended.py tests/test_permission_guard.py tests/test_feishu_approval_config.py tests/test_hermes_entry.py -q`，`94 passed, 14 warnings`。
- 2026-05-19：review-agent 复审发现文档状态不一致与 `runtime/feishu_approval_overlay.py` 仍保留 `HERMES_CONFIG_PATH`；已修复文档状态并统一 overlay 配置路径。
- 2026-05-19：test-agent 为 `runtime/feishu_approval_overlay.py` 补 `HERMES_CONFIG` 优先、`HERMES_HOME` fallback、`HERMES_CONFIG_PATH` 不再生效、无 env 不读当前目录 `config.yaml` 回归；`rtk python3 -m pytest tests/test_feishu_approval_overlay.py -q`，`21 passed`。
- 2026-05-19：dev-lead-agent fresh 验证：`rtk python3 -m pytest tests/test_alert_webhook.py tests/test_approval_async.py tests/test_approval_reply.py tests/test_identity_extended.py tests/test_permission_guard.py tests/test_feishu_approval_config.py tests/test_hermes_entry.py tests/test_feishu_approval_overlay.py -q`，`115 passed, 14 warnings`；静态扫描确认运行时代码无旧配置入口残留，仓库根 `config.yaml` 不存在。
