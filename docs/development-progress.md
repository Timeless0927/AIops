# 开发进度历史与长期能力索引

最后状态快照：2026-05-18
事实源迁移：2026-05-20

> 本文件不再作为当前开发进度、阻塞项或验收结论的事实源。2026-05-20 起，AIops MVP 的任务状态、阻塞、验收材料、剩余风险和完成结论以 Multica issue 为准。
>
> 当前 MVP 总体事实源是 `AIO-12`；剩余工作已拆分到 `AIO-13` 至 `AIO-18`。本文件保留为长期能力索引、历史验证记录和代码/测试证据导航，帮助后续开发理解已有能力，避免重复扫描。

## 状态定义

- `完成`：生产代码已落地，关键路径有测试覆盖，最近一次验证通过。
- `部分完成`：已有可复用代码或工具原语，但未达到端到端验收标准。
- `未开发`：没有生产实现，或只有设计文档/占位说明。

## 维护规则（迁移后）

1. 开发开始前可参考本文件确认已有能力、代码/测试证据和历史验证记录；实时状态必须读取对应 Multica issue。
2. 开发完成、测试通过、验收失败、阻塞或范围调整时，必须回写对应 Multica issue，不再要求同步本文件作为事实源。
3. 只有长期能力边界、产品/架构/测试知识或代码/测试证据索引发生稳定变化时，才更新本文件。
4. 本文件中的历史 `状态` 和 `剩余工作` 表示 2026-05-18/2026-05-19 附近的快照；若与 Multica issue 冲突，以 issue 为准。
5. agent 最终回复应说明修改的文档和 issue 回写结果；无需声明“是否更新本文件”，除非本文件本身被修改。

## Multica issue 映射

| 历史剩余工作 / 验收缺口 | 当前事实源 |
|---|---|
| MVP 总体目标、成功定义、协作规则 | `AIO-12` |
| 真实 `FEISHU_APPROVAL_CODE`、飞书审批中心、事件订阅、polling 验收 | `AIO-13` |
| 审批后真实 Kubernetes 安全执行、dry-run、operation lock、health check、Feishu thread 结果通知 | `AIO-14` |
| `rollback_required` 状态、通知、execution coordinator/store 接线和确定性 rollback 验收 | `AIO-15` |
| K8s 部署、runtime config 单一来源、PVC 持久化、Pod 滚动重启验收 | `AIO-16` |
| 本地状态文档迁移为长期文档，项目状态改由 Multica issue 承载 | `AIO-17` |
| 真实飞书群/线程、审批入口可见性、旧 pending 补发/补回写验收 | `AIO-18` |

## 历史总览

当前边界：系统已完成 `Alertmanager -> incident -> analysis -> incident Feishu binding -> approval card delivery/writeback 或 Feishu native approval -> approval/timeline 状态更新` 的本地自动化验证。CR-2026-05-15-001 已完成本地实现、测试和复审；审批通过后的安全自动执行闭环仍在开发中，真实 Feishu 群/线程、真实飞书审批中心和真实事件订阅端到端验收仍需补跑。
现场排查补充：当前 Pod 注入的 `HERMES_CONFIG=/data/hermes/config.yaml` 需要被 `hooks/alert_webhook.py` 和 `toolsets/approval_async.py` 正确消费；否则会误读仓库根 `config.yaml`，把 native approval 降级成旧卡片审批。
本地只测试审批/Feishu 卡片时，可设置 `AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED=0` 跳过 approval execution worker；默认未设置仍启用生产行为。
部署入口现在会把 `sre_permissions`、`approval_policy`、Feishu 群消息默认策略和飞书原生审批开关/`FEISHU_APPROVAL_CODE`/申请人 open_id 一起渲染进 `~/.hermes/config.yaml`，K8S runtime 不再依赖仓库根 `config.yaml` 的审批人列表，容器默认 `FEISHU_GROUP_POLICY=open` 以支持群聊 @ 响应。

参考文档：
- `docs/hermes-sre-agent-architecture.md`
- `docs/superpowers/plans/2026-05-06-phase3-alert-diagnosis-approval.md`
- `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md`

最近验证：
- 2026-05-18：最终复验通过：`tests/test_alert_webhook_server.py tests/test_data_dir_env.py` 3 passed；`tests/test_approval_async.py` 27 passed；主目标 focused tests 95 passed, 15 warnings；`config.yaml` 与 `deploy/hermes-config.template.yaml` YAML 解析通过；review-agent 复审无阻断。
- 2026-05-15：飞书原生审批接入处于计划阶段，已更新 CR、实施计划和 TDD 测试计划；尚未运行实现验证。
- 2026-05-11：审批卡片强制投递与回写：`python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q`，68 passed，14 warnings。
- 2026-05-11：PVC 持久化路径切换：`pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q`，21 passed。
- 2026-05-09：容器 Feishu 群消息策略修复：`rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q`，9 passed。
- 2026-05-07：`pytest tests/ -q`，204 passed，13 warnings。
- 2026-05-08：worker B2 focused：`pytest tests/test_remediation_execution.py tests/test_k8s_tools.py tests/test_remediation_plan.py -q`，18 passed；`pytest tests/test_approval_execution.py -q`，8 passed。
- 2026-05-08：worker A2 focused：`pytest tests/test_approval_execution.py tests/test_approval_async.py -q`，14 passed；`pytest tests/test_remediation_execution.py -q`，7 passed。
- 2026-05-08：worker B3 focused：`pytest tests/test_remediation_execution.py -q`，9 passed；`pytest tests/test_approval_execution.py -q`，8 passed。
- 2026-05-08：worker A3 focused：`pytest tests/test_approval_execution_worker.py tests/test_feishu_approval_overlay.py tests/test_approval_reply.py -q`，17 passed；`pytest tests/test_approval_execution.py tests/test_remediation_execution.py -q`，17 passed。
- 2026-05-08：worker A4 focused：`pytest tests/test_approval_execution_worker.py tests/test_feishu_approval_overlay.py tests/test_approval_execution.py -q`，19 passed。
- 2026-05-08：worker C2 health/rollback reviewer 修复：`python3 -m pytest tests/test_remediation_health.py tests/test_remediation_rollback.py tests/test_message_delivery.py tests/test_incident_store.py tests/test_remediation_execution.py -q`，35 passed。
- 2026-05-08：worker C3/B4 health/rollback adapter 接线：`python3 -m pytest tests/test_remediation_execution.py tests/test_remediation_health.py tests/test_message_delivery.py tests/test_incident_store.py tests/test_approval_execution.py -q`，43 passed。
- 2026-05-08：worker D Feishu card buttons focused：`pytest tests/test_approval_reply.py tests/test_feishu_approval_overlay.py tests/test_approval_execution_worker.py -q`，29 passed。
- 2026-05-08：worker FixLocalApprovalTest focused：`python3 -m pytest tests/test_approval_execution_worker.py tests/test_feishu_approval_overlay.py tests/test_approval_reply.py -q`，31 passed。
- 2026-05-08：worker FixFeishuCardResolvedState focused：`python3 -m pytest tests/test_feishu_approval_overlay.py -q`，15 passed；`python3 -m pytest tests/test_approval_execution_worker.py tests/test_feishu_approval_overlay.py tests/test_approval_reply.py -q`，31 passed。
- 2026-05-08：worker D Feishu card readable approver focused：`pytest tests/test_feishu_approval_overlay.py -q`，17 passed。
- 2026-05-08：worker D Feishu card runtime config resolver focused：`pytest tests/test_feishu_approval_overlay.py -q`，17 passed。
- 2026-05-08：worker D Feishu approval runtime authorization focused：`pytest tests/test_approval_reply.py tests/test_feishu_approval_overlay.py -q`，31 passed。
- 2026-05-08：worker D identity config fallback focused：`pytest tests/test_identity_extended.py tests/test_approval_reply.py tests/test_feishu_approval_overlay.py -q`，38 passed。
- 2026-05-08：worker D identity HERMES_CONFIG_PATH focused：`pytest tests/test_identity_extended.py tests/test_approval_reply.py tests/test_feishu_approval_overlay.py -q`，40 passed。

## 功能进度

| 模块 | 功能 | 状态 | 代码/测试证据 | 剩余工作 |
|---|---|---|---|---|
| Alert intake | Alertmanager webhook 接入与 HMAC 校验 | 完成 | `hooks/alert_webhook.py`, `tests/test_alert_webhook.py`, `tests/test_alert_webhook_server.py` | 仅剩 aiohttp `NotAppKeyWarning` 清理，不阻塞功能 |
| Alert intake | 告警去重与 incident 创建/复用 | 完成 | `hooks/alert_webhook.py`, `toolsets/alert_dedup.py`, `toolsets/incident_store.py`, `tests/test_alert_webhook.py`, `tests/test_alert_dedup.py`, `tests/test_incident_store.py` | 后续可把内存态去重升级为跨进程持久化 |
| Diagnosis | K8s targeted evidence 采集 | 完成 | `hooks/alert_webhook.py`, `tests/test_alert_webhook.py` | 后续可扩展更多 workload 类型 |
| Diagnosis | 分析结果持久化与相似案例召回 | 完成 | `hooks/alert_webhook.py`, `toolsets/incident_store.py`, `tests/test_alert_webhook.py`, `tests/test_incident_store.py` | 后续可提升召回排序质量 |
| Feishu | Feishu thread summary 与 incident 绑定 | 完成 | `hooks/alert_webhook.py`, `hooks/feishu_conversation.py`, `tests/test_alert_webhook.py`, `tests/test_feishu_conversation.py` | 审批交互卡片投递见下一行 |
| Feishu | 审批卡片强制投递与 `approval_message_id` 回写 | 完成 | `toolsets/approval_async.py`, `toolsets/message_delivery.py`, `hooks/feishu_conversation.py`, `hooks/alert_webhook.py`, `hooks/recovery.py`, `runtime/feishu_approval_overlay.py`, `tests/test_approval_async.py`, `tests/test_message_delivery.py`, `tests/test_feishu_conversation.py`, `tests/test_alert_webhook.py`, `tests/test_recovery.py`, `tests/test_feishu_approval_overlay.py` 覆盖工具返回、线程卡片、旧 pending 补发、startup recovery、sent outbox 补回写；2026-05-18 focused tests 通过 | 真实 Feishu 群/线程端到端验收仍待补跑 |
| Feishu | 飞书原生审批主路径 | 部分完成 | `toolsets/feishu_native_approval.py`, `hooks/feishu_approval_event.py`, `hooks/alert_webhook.py`, `hooks/recovery.py`, `toolsets/approval_async.py`, `tests/test_feishu_native_approval.py`, `tests/test_feishu_approval_event.py`, `tests/test_feishu_approval_config.py`, `tests/test_approval_async.py`, `tests/test_alert_webhook.py`, `tests/test_alert_webhook_server.py`, `tests/test_recovery.py`, `docs/adr/0001-feishu-native-approval.md` 覆盖原生审批实例、事件 webhook、外部状态字段、polling 补偿、执行门禁、自定义审批卡片降级和 import shadowing 修复 | 待真实 `FEISHU_APPROVAL_CODE`、飞书审批中心、事件订阅和 polling 端到端验收 |
| Feishu | 运行时配置来源统一 | 完成 | `hooks/alert_webhook.py`, `hooks/identity.py`, `runtime/feishu_approval_overlay.py`, `toolsets/alert_dedup.py`, `toolsets/cost_guard.py`, `toolsets/llm_fallback.py`, `toolsets/notification_manager.py`, `toolsets/query_guard.py`, `toolsets/skill_promotion.py`, `toolsets/approval_async.py`, `tests/test_alert_webhook.py`, `tests/test_approval_async.py`, `tests/test_approval_reply.py`, `tests/test_identity_extended.py`, `tests/test_permission_guard.py`, `tests/test_feishu_approval_config.py`, `tests/test_feishu_approval_overlay.py`, `tests/test_hermes_entry.py` 覆盖 `HERMES_CONFIG -> HERMES_HOME/config.yaml` 单一来源、删除 repo-root `config.yaml` 后的回归、`HERMES_CONFIG_PATH` 不再生效、native approval 分支和相关授权/守卫读取；2026-05-19 focused tests：94 passed + overlay 21 passed | 仍待全量套件与真实集群验收 |
| Phase 3 approval MVP | 从 `next_best_actions` 创建 pending approval | 完成 | `hooks/alert_webhook.py`, `toolsets/approval_async.py`, `tests/test_alert_webhook.py`, `tests/test_approval_async.py` | 无 |
| Phase 3 approval MVP | 审批持久化、幂等、过期处理 | 完成 | `toolsets/approval_async.py`, `toolsets/sre_metrics.py`, `hooks/recovery.py`, `tests/test_approval_async.py`, `tests/test_sre_metrics.py`, `tests/test_recovery.py` | 无 |
| Phase 3 approval MVP | Feishu 文本审批回复：`批准 <approval_id>` / `拒绝 <approval_id> <reason>` | 完成 | `hooks/approval_reply.py`, `tests/test_approval_reply.py` | 无 |
| Runtime overlay | 拦截 Feishu 审批文本，避免进入 LLM | 完成 | `runtime/feishu_approval_overlay.py`, `runtime/hermes_gateway.py`, `tests/test_feishu_approval_overlay.py` | 无 |
| Authorization | 审批人授权、命名空间权限、自审批限制、fail closed | 完成 | `hooks/approval_authorization.py`, `hooks/approval_reply.py`, `tests/test_approval_authorization.py`, `tests/test_approval_reply.py` | 无 |
| Remediation schema | 结构化 `remediation_action` 与 `action_signature` | 完成 | `toolsets/remediation_plan.py`, `hooks/alert_webhook.py`, `tests/test_alert_webhook.py` | 后续扩展更多 remediation action 类型与真实集群验收 |
| Observability | approval backlog / expired approval / expired lock metrics | 完成 | `toolsets/sre_metrics.py`, `tests/test_sre_metrics.py` | 后续补执行成功率、回滚率指标 |
| Safety primitive | operation lock 基础工具 | 完成 | `toolsets/operation_lock.py`, `tests/test_operation_lock.py` | remediation execution adapter 已接入 acquire/release；后续补锁冲突可视化与并发场景验收 |
| Safety primitive | `k8s_write` / `k8s_exec` guard 与审批级别判定 | 部分完成 | `toolsets/k8s_write.py`, `toolsets/k8s_exec.py`, `toolsets/k8s_guard.py`, `toolsets/remediation_execution.py`, `tests/test_k8s_tools.py`, `tests/test_k8s_guard.py`, `tests/test_remediation_execution.py` | remediation adapter 通过 `k8s_write.execute_approved` 发起 server-side dry-run/执行，并已接入 health/rollback_required；仍缺真实集群验收，`k8s_exec` 不进自动执行 V1 |
| Approval execution | approved approval 执行 coordinator 持久化/幂等 | 部分完成 | `toolsets/approval_execution.py`, `toolsets/approval_async.py` 的 `approval_executions` 表, `toolsets/remediation_execution.py` validator/signature/health adapter 复用, `runtime/approval_execution_worker.py`, `runtime/hermes_gateway.py`, `tests/test_approval_execution.py`, `tests/test_remediation_execution.py`, `tests/test_approval_execution_worker.py`, `tests/test_feishu_approval_overlay.py`, `tests/test_approval_reply.py` 覆盖 approved-only、默认 adapter fail closed、queued CAS claim、签名复算、重复幂等、持久化状态、显式 adapter 注入、production worker trigger、startup cutoff 避免历史 approved 补执行、worker 启动失败不阻断 gateway、approval reply 不直接执行、health rollback_required 不标 executed | production worker trigger 已在 gateway overlay install 后接线并显式注入真实 adapter，且默认只消费 worker 启动 cutoff 后批准的审批；仍缺真实集群执行和 Feishu card 验收 |
| Approval execution | server-side dry-run adapter | 部分完成 | `toolsets/remediation_execution.py`, `tests/test_remediation_execution.py` 覆盖命令构造、dry-run 失败短路、adapter stage 语义拆分（dry_run_action 只 dry-run，execute_action 才真实执行）、health healthy/rollback_required/fail-closed | 仍待真实集群验收；未实现 client dry-run fallback |
| Approval execution | 审批后真实安全执行链路 | 部分完成 | `toolsets/remediation_execution.py` 串联 validate、dry-run、operation lock、execute、audit、incident timeline、health check、rollback_required 记录/通知，并提供 `create_approval_execution_adapter()`；`runtime/approval_execution_worker.py` production trigger 显式注入 adapter 并带 startup cutoff；`tests/test_remediation_execution.py`, `tests/test_approval_execution_worker.py` 覆盖 stage 顺序、dry-run/execute 分离、health rollback_required 阻断 mark executed 与 worker tick | production worker trigger 已接线且避免历史审批补执行；仍待真实集群、Feishu card 验收 |
| Approval execution | 执行后健康检查 | 部分完成 | `toolsets/remediation_health.py`, `toolsets/remediation_execution.py`, `tests/test_remediation_health.py`, `tests/test_remediation_execution.py` 覆盖 rollout/replica 成功失败、adapter 调用 `check_and_record_action_health()`、无 incident fail closed、`pending_approval` 健康失败进入 `rollback_required` | 缺真实集群 rollout 验收和 Feishu card 验收路径 |
| Approval execution | `rollback_required` 通知 | 部分完成 | `toolsets/incident_store.py`, `toolsets/message_delivery.py`, `toolsets/remediation_health.py`, `toolsets/remediation_execution.py`, `tests/test_incident_store.py`, `tests/test_message_delivery.py`, `tests/test_remediation_health.py`, `tests/test_remediation_execution.py` 覆盖状态、timeline、通知排队、`previous_status` 审计元数据、adapter rollback_required 接线 | 缺 Feishu 实际发送 worker/card 验收 |
| Approval execution | 确定性 rollback | 部分完成 | `toolsets/remediation_rollback.py`, `tests/test_remediation_rollback.py` 覆盖 scale deployment previous replicas rollback、schema/risk/cluster fail-closed、dry-run、operation lock、audit、rollback timeline | 缺 execution store/coordinator 接入、真实集群验收 |
| Feishu UX | Feishu 卡片按钮审批 | 部分完成 | `runtime/feishu_approval_overlay.py`, `hooks/approval_reply.py`, `hooks/identity.py`, `tests/test_feishu_approval_overlay.py`, `tests/test_approval_reply.py`, `tests/test_identity_extended.py` 覆盖 card payload、approve/reject callback、同步 raw callback card 更新原卡片并移除按钮、提交态和授权都从 Hermes runtime config 解析 Feishu operator、identity config env override/repo fallback/missing fail-closed、缺字段 fail closed、未授权/已决不 mutate、文本审批兼容、callback 不直接执行 remediation | 仍缺真实 Feishu 平台更新后二次验收 |
| Knowledge loop | Skill 动态闭环基础工具 | 完成 | `toolsets/skill_extractor_tool.py`, `tests/test_skill_extractor_tool.py`, `skills/sre/*` | 后续接专家审核和上线流程 |
| Deployment | K8s 部署 manifests / AIOps image | 部分完成 | `Dockerfile.aiops`, `deploy/entrypoint.sh`, `deploy/hermes-config.template.yaml`, `deploy/k8s/*`, `tests/test_deploy_entrypoint.py`, `tests/test_k8s_manifests.py`, `tests/test_data_dir_env.py`, `toolsets/cost_guard.py`, `toolsets/rejection_learner.py` | runtime config 已对齐 Feishu operator/approval policy、群消息默认策略、飞书原生审批 env 渲染、申请人 open_id 与 PVC 持久化路径（`/data/hermes` + `/data/aiops`）；仍缺完整发布流水线和多环境验证 |
| Multi-tenant ops | 多实例/多团队生产化 | 部分完成 | `docs/feishu-sre-agent-deployment-plan.md`, `docs/feishu-sre-agent-detailed-design.md` | 缺生产级多团队隔离、横向扩展验收 |

## 历史下一步开发顺序

按 `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md` 继续：

1. `Approval execution coordinator`：消费 approved approval，保证幂等，只做状态流转和审计，不急着真实写 K8s。
2. `server-side dry-run adapter`：执行前 dry-run，失败则短路并通知。
3. `safe execution API`：接入 operation lock、audit log、incident timeline。
4. `health check`：执行后验证 rollout/replica 状态。
5. `rollback_required` 与确定性 rollback。
6. Feishu card buttons。

以上顺序是历史实施计划，不再作为当前排期事实源。当前执行顺序、阻塞和验收结论以 `AIO-12` 及其子 issue 为准。

## 未纳入当前阶段

| 项目 | 原因 |
|---|---|
| Helm / ArgoCD 写操作 | 当前先完成 kubectl 安全闭环，避免扩大 blast radius |
| 多集群调度和队列化执行 | 早期瓶颈是安全与可恢复性，不是吞吐 |
| 自动 cron 巡检 | 与 alert-to-approval 主链路独立，适合单独 workstream |
