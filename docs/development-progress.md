# 开发进度表

最后更新：2026-05-07

本文件是开发进度事实源。以后每个 agent 完成功能开发、补测试、调整范围或确认未开发项时，必须在同一个变更中更新此表，避免下一次接手时重新全量扫描代码。

## 状态定义

- `完成`：生产代码已落地，关键路径有测试覆盖，最近一次验证通过。
- `部分完成`：已有可复用代码或工具原语，但未达到端到端验收标准。
- `未开发`：没有生产实现，或只有设计文档/占位说明。

## 维护规则

1. 开发开始前先读本文件，确认不要重复建设。
2. 开发完成后必须更新对应行的 `状态`、`代码/测试证据`、`剩余工作` 和 `最近验证`。
3. 只有代码、测试、验收路径都完成时，才能把状态改为 `完成`。
4. 新增功能必须新增一行；不要只写在聊天记录、提交信息或设计文档里。
5. agent 最终回复必须说明是否更新了本文件；如果没有更新，必须说明原因。

## 当前总览

当前边界：系统已完成 `Alertmanager -> incident -> analysis -> pending approval -> Feishu text approval -> approval/timeline 状态更新`。审批通过后的安全自动执行闭环仍在开发中。

参考文档：
- `docs/hermes-sre-agent-architecture.md`
- `docs/superpowers/plans/2026-05-06-phase3-alert-diagnosis-approval.md`
- `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md`

最近验证：
- 2026-05-07：`pytest tests/ -q`，204 passed，13 warnings。

## 功能进度

| 模块 | 功能 | 状态 | 代码/测试证据 | 剩余工作 |
|---|---|---|---|---|
| Alert intake | Alertmanager webhook 接入与 HMAC 校验 | 完成 | `hooks/alert_webhook.py`, `tests/test_alert_webhook.py`, `tests/test_alert_webhook_server.py` | 仅剩 aiohttp `NotAppKeyWarning` 清理，不阻塞功能 |
| Alert intake | 告警去重与 incident 创建/复用 | 完成 | `hooks/alert_webhook.py`, `toolsets/alert_dedup.py`, `toolsets/incident_store.py`, `tests/test_alert_webhook.py`, `tests/test_alert_dedup.py`, `tests/test_incident_store.py` | 后续可把内存态去重升级为跨进程持久化 |
| Diagnosis | K8s targeted evidence 采集 | 完成 | `hooks/alert_webhook.py`, `tests/test_alert_webhook.py` | 后续可扩展更多 workload 类型 |
| Diagnosis | 分析结果持久化与相似案例召回 | 完成 | `hooks/alert_webhook.py`, `toolsets/incident_store.py`, `tests/test_alert_webhook.py`, `tests/test_incident_store.py` | 后续可提升召回排序质量 |
| Feishu | Feishu thread summary 与 incident 绑定 | 完成 | `hooks/alert_webhook.py`, `hooks/feishu_conversation.py`, `tests/test_alert_webhook.py`, `tests/test_feishu_conversation.py` | 后续可补交互卡片 |
| Phase 3 approval MVP | 从 `next_best_actions` 创建 pending approval | 完成 | `hooks/alert_webhook.py`, `toolsets/approval_async.py`, `tests/test_alert_webhook.py`, `tests/test_approval_async.py` | 无 |
| Phase 3 approval MVP | 审批持久化、幂等、过期处理 | 完成 | `toolsets/approval_async.py`, `toolsets/sre_metrics.py`, `hooks/recovery.py`, `tests/test_approval_async.py`, `tests/test_sre_metrics.py`, `tests/test_recovery.py` | 无 |
| Phase 3 approval MVP | Feishu 文本审批回复：`批准 <approval_id>` / `拒绝 <approval_id> <reason>` | 完成 | `hooks/approval_reply.py`, `tests/test_approval_reply.py` | 无 |
| Runtime overlay | 拦截 Feishu 审批文本，避免进入 LLM | 完成 | `runtime/feishu_approval_overlay.py`, `runtime/hermes_gateway.py`, `tests/test_feishu_approval_overlay.py` | 无 |
| Authorization | 审批人授权、命名空间权限、自审批限制、fail closed | 完成 | `hooks/approval_authorization.py`, `hooks/approval_reply.py`, `tests/test_approval_authorization.py`, `tests/test_approval_reply.py` | 无 |
| Remediation schema | 结构化 `remediation_action` 与 `action_signature` | 完成 | `toolsets/remediation_plan.py`, `hooks/alert_webhook.py`, `tests/test_alert_webhook.py` | 后续执行链路消费该 schema |
| Observability | approval backlog / expired approval / expired lock metrics | 完成 | `toolsets/sre_metrics.py`, `tests/test_sre_metrics.py` | 后续补执行成功率、回滚率指标 |
| Safety primitive | operation lock 基础工具 | 完成 | `toolsets/operation_lock.py`, `tests/test_operation_lock.py` | 未接入审批后执行 coordinator |
| Safety primitive | `k8s_write` / `k8s_exec` guard 与审批级别判定 | 部分完成 | `toolsets/k8s_write.py`, `toolsets/k8s_exec.py`, `toolsets/k8s_guard.py`, `tests/test_k8s_tools.py`, `tests/test_k8s_guard.py` | 缺 server-side dry-run、执行审计、健康检查、回滚接线 |
| Approval execution | approved approval 执行 coordinator 持久化/幂等 | 部分完成 | `toolsets/approval_async.py` 有 `executed_at` 和 `execute_approved` 状态标记 | 缺独立 coordinator、执行记录表、重复消费保护测试 |
| Approval execution | server-side dry-run adapter | 未开发 | 仅设计文档存在 | 实现 dry-run 命令构造、失败短路、测试 |
| Approval execution | 审批后真实安全执行链路 | 未开发 | 仅 `k8s_write.execute_approved` / `k8s_exec.execute_approved` 原语存在 | 串联 authorization、dry-run、lock、execute、audit、timeline、notification |
| Approval execution | 执行后健康检查 | 未开发 | 仅设计文档存在 | 实现 rollout/replica 健康检查与失败状态 |
| Approval execution | `rollback_required` 通知 | 部分完成 | `toolsets/message_delivery.py` 有 delivery 基础设施；文档定义了状态 | 缺 incident 状态接线、通知模板、测试 |
| Approval execution | 确定性 rollback | 未开发 | 仅设计文档存在 | 实现 selected action rollback，比如 scale deployment 回滚副本数 |
| Feishu UX | Feishu 卡片按钮审批 | 未开发 | 仅设计文档存在 | 实现 card payload、callback 鉴权、兼容文本审批 |
| Knowledge loop | Skill 动态闭环基础工具 | 完成 | `toolsets/skill_extractor_tool.py`, `tests/test_skill_extractor_tool.py`, `skills/sre/*` | 后续接专家审核和上线流程 |
| Deployment | K8s 部署 manifests / AIOps image | 部分完成 | `Dockerfile.aiops`, `deploy/k8s/*`, `tests/test_deploy_entrypoint.py` | 缺完整发布流水线和多环境验证 |
| Multi-tenant ops | 多实例/多团队生产化 | 部分完成 | `docs/feishu-sre-agent-deployment-plan.md`, `docs/feishu-sre-agent-detailed-design.md` | 缺生产级多团队隔离、横向扩展验收 |

## 下一步开发顺序

按 `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md` 继续：

1. `Approval execution coordinator`：消费 approved approval，保证幂等，只做状态流转和审计，不急着真实写 K8s。
2. `server-side dry-run adapter`：执行前 dry-run，失败则短路并通知。
3. `safe execution API`：接入 operation lock、audit log、incident timeline。
4. `health check`：执行后验证 rollout/replica 状态。
5. `rollback_required` 与确定性 rollback。
6. Feishu card buttons。

## 未纳入当前阶段

| 项目 | 原因 |
|---|---|
| Helm / ArgoCD 写操作 | 当前先完成 kubectl 安全闭环，避免扩大 blast radius |
| 多集群调度和队列化执行 | 早期瓶颈是安全与可恢复性，不是吞吐 |
| 自动 cron 巡检 | 与 alert-to-approval 主链路独立，适合单独 workstream |
