# TDD 测试计划

## 来源输入

- `docs/01-BDD.md`
- `docs/02-DDD.md`
- `docs/03-SDD.md`
- `docs/CHANGE-REQUESTS.md` 中已批准的变更
- `CR-2026-05-11-002：审批卡片强制投递与回写`
- `CR-2026-05-15-001：接入飞书原生审批`

## 测试策略

- BDD 场景转为验收测试或集成测试。
- DDD 不变量转为领域单元测试。
- SDD 契约转为 API、集成或契约测试。
- bug 修复优先从失败的回归测试开始。
- 这次修复先锁定三条回归：新审批必须拿到 `approval_message_id`、旧 pending 必须可补发、Alertmanager 顺序必须先绑定 incident 再发审批卡片。
- 飞书原生审批接入必须按 Red-Green-Refactor 推进：先补飞书实例创建、事件同步、状态机幂等、执行门禁、告警入口、polling 补偿失败测试，再实现最小代码。

## 测试矩阵

| 行为 / 不变量 | 测试类型 | 测试文件 | 状态 |
| --- | --- | --- | --- |
| `update_approval_message_id` 能回写审批卡片消息 ID | 单元 | `tests/test_approval_async.py` | 已通过 |
| `publish_approval_card` 能按 incident 线程发送 interactive card | 单元 | `tests/test_feishu_conversation.py` | 已通过 |
| `build_approval_card_payload` 带 requester/approve/deny 按钮值 | 单元 | `tests/test_feishu_approval_overlay.py` | 已通过 |
| `sre_request_approval` 返回 delivery_status 与非空 approval_message_id | 集成 | `tests/test_approval_async.py` | 已通过 |
| Alertmanager 先回写 incident binding，再创建/补发审批卡片 | 集成 / 验收 | `tests/test_alert_webhook.py` | 已通过 |
| startup recovery 扫描 pending 且无 approval_message_id 的审批并补发 | 集成 | `tests/test_recovery.py` | 已通过 |
| sent outbox 已记录 `target_message_id` 但 approval 未回写时，只补回写不重复发卡 | 集成 | `tests/test_approval_async.py`, `tests/test_message_delivery.py` | 已通过 |
| 创建飞书原生审批实例成功后保存 `instance_code`、审批链接和外部状态 | 单元 / 集成 | `tests/test_feishu_native_approval.py`, `tests/test_approval_async.py` | 已通过 |
| 飞书返回错误、token 获取失败、HTTP 超时、非 JSON 响应都有明确错误并让本地状态进入 `approval_create_failed` | 单元 | `tests/test_feishu_native_approval.py`, `tests/test_alert_webhook.py` | 已通过 |
| 飞书 `APPROVED` 事件把 `external_pending` 幂等同步为 `approved`，但不直接执行命令 | 集成 | `tests/test_feishu_approval_event.py`, `tests/test_approval_async.py` | 已通过 |
| 飞书 `REJECTED` / `CANCELED` 事件分别同步为 `denied` / `canceled` | 集成 | `tests/test_feishu_approval_event.py` | 已通过 |
| 重复事件、未知 `uuid` / `instance_code` 只记录审计，不重复触发执行 | 集成 | `tests/test_feishu_approval_event.py` | 已通过 |
| 非法签名、非法 token 或非法事件来源被拒绝 | 单元 | `tests/test_feishu_approval_event.py` | 已通过 |
| 旧数据库迁移后 approval 表具备外部审批字段，历史记录仍可读 | 单元 | `tests/test_approval_async.py` | 已通过 |
| `resolve_external_approval(...)` 不允许已 `executed` / `failed` / `denied` / `canceled` / `expired` 的 approval 被覆盖 | 单元 | `tests/test_approval_async.py` | 已通过 |
| 只有本地 `approved` 状态能进入 execution worker，`external_pending` 和 `approval_create_failed` 不能执行 | 集成 | `tests/test_approval_async.py`, `tests/test_recovery.py` | 已通过 |
| 需要审批的告警修复动作创建飞书原生审批，创建失败时不会执行动作 | 集成 / 验收 | `tests/test_alert_webhook.py` | 已通过 |
| thread 中回写审批链接、风险摘要和操作摘要，自定义审批卡片不作为主入口 | 集成 | `tests/test_alert_webhook.py`, `tests/test_feishu_conversation.py` | 已通过 |
| pending 原生审批可被 polling worker 补偿同步，webhook 丢失时最终同步 approved/rejected | 集成 | `tests/test_recovery.py` | 已通过 |

## Red-Green-Refactor 记录

| 日期 | 测试 | Red 证据 | Green 证据 | 重构说明 |
| --- | --- | --- | --- | --- |
| 2026-05-11 | 审批卡片投递与回写回归 | review-agent 先后指出工具返回契约、Alertmanager 投递状态、sent outbox 非原子窗口缺口 | `python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q`，68 passed, 14 warnings | 补 `_request_handler` 返回契约、旧 pending 补发、sent outbox 补回写测试 |
| 2026-05-15 | 飞书原生审批接入 | test-agent 先补 Red 测试，初始 focused 结果 `30 failed, 42 passed, 14 warnings` | implementation-agent 完成后 focused 结果 `72 passed, 14 warnings`，py_compile 通过 | 剩余真实飞书审批中心端到端验收 |
| 2026-05-18 | 外部审批边界与 import shadowing 回归 | review-agent 指出普通本地 `pending` 可被外部飞书事件误批准；test-agent 复现 `toolsets` 被 `hermes-agent/toolsets.py` 遮蔽 | `tests/test_approval_async.py` 27 passed；`tests/test_alert_webhook_server.py tests/test_data_dir_env.py` 3 passed；主目标 focused tests 95 passed, 15 warnings | 补非 `external_pending` 外部事件拒绝测试；修复 Hermes registry import 后恢复本仓库 `toolsets` namespace |

## 人工验证

| 流程 | 步骤 | 结果 |
| --- | --- | --- |
| 飞书卡片可见性 | 创建新审批，确认返回 `approval_message_id` 非空，飞书线程里能看到卡片 | 本地自动化已覆盖发送调用和返回契约；真实飞书群/线程待验收 |
| 历史坏状态补发 | 人工构造 `status=pending` 且 `approval_message_id=null` 的审批，触发 recovery 或手动补发 | 自动化已覆盖 |
| 告警主流程顺序 | 触发 Alertmanager webhook，确认 incident 先绑定，再发审批卡片 | 自动化已覆盖 |
| 飞书原生审批端到端 | 使用真实 `FEISHU_APPROVAL_CODE` 创建审批实例，在飞书审批中心批准/拒绝，确认 AIOps 本地状态同步且只有 approved 进入执行 | 待真实飞书环境验收 |
| webhook 丢失补偿 | 暂停或丢弃审批事件 webhook，等待 polling worker 查询实例状态并同步本地 approval | 待真实或集成环境验收 |
