# 实施计划

## 来源输入

- `docs/03-SDD.md`
- `docs/CHANGE-REQUESTS.md` 中已批准的变更
- `CR-2026-05-11-002：审批卡片强制投递与回写`
- `CR-2026-05-15-001：接入飞书原生审批`

## 当前目标

把审批主路径从自定义飞书审批卡片升级为飞书原生审批：本地 approval 创建后进入 `external_pending`，创建飞书审批实例并保存外部字段，飞书审批事件或 polling 补偿同步本地状态；只有本地 `approved` 才能进入 execution worker。

## 任务清单

- [x] `toolsets/approval_async.py`：补 `approval_message_id` 回写，新增审批卡片投递/补发协调函数，保持 `sre_request_approval` 的工具返回不再把 `ok=true` 误解成“卡片已可见”。
- [x] `hooks/feishu_conversation.py`：新增 `publish_approval_card(...)`，优先回复到 incident 线程，卡片正文带 `incident_id`、`approval_id`、`operation_type`、`namespace`、`risk_level`、`command`、`requester`。
- [x] `runtime/feishu_approval_overlay.py`：补齐审批卡片 payload 的 `requester` 字段，按钮 value 继续复用 `approval_decision`。
- [x] `hooks/alert_webhook.py`：先发 incident 状态消息并回写飞书绑定，再创建/补发审批卡片；已有 `pending` 且 `approval_message_id` 为空时必须补发。
- [x] `hooks/recovery.py`：在 startup 恢复里扫描旧的 pending 审批，尝试补发卡片后再执行过期处理。
- [x] `toolsets/message_delivery.py`：补 sent outbox 查询，用于发送成功但 approval 回写失败时补回写并避免重复发卡。
- [x] `tests/test_approval_async.py`、`tests/test_feishu_conversation.py`、`tests/test_alert_webhook.py`、`tests/test_recovery.py`、`tests/test_feishu_approval_overlay.py`、`tests/test_message_delivery.py`：补回归。
- [x] `docs/development-progress.md`、`docs/TODD.md`：实现后同步状态和证据。
- [x] `toolsets/approval_async.py`：增加外部审批字段迁移、`external_pending` / `approval_create_failed` 状态、`resolve_external_approval(...)` 幂等同步入口，保证终态和已执行状态不能被外部事件回滚。
- [x] `toolsets/feishu_native_approval.py`：新增飞书原生审批 OpenAPI 客户端，创建 `POST /approval/v4/instances`，传入 `approval_code`、`requester_open_id`、`uuid=approval_id` 和审批表单 JSON，处理 token、超时、非 JSON 和飞书错误响应。
- [x] `hooks/alert_webhook.py`：需要审批时创建本地 approval 后调用飞书原生审批；创建失败进入 `approval_create_failed` 且不得执行；thread 仅通知审批已发起、审批链接、风险摘要和操作摘要。
- [x] `hooks/feishu_approval_event.py`：新增飞书审批实例状态变更 webhook，校验事件来源，解析 `uuid`、`instance_code`、`status`，只调用本地同步入口，不直接执行命令。
- [x] `hooks/alert_webhook_server.py`：在实际 webhook 服务入口注册 `/webhooks/feishu/approval`，确保飞书事件 callback 可达。
- [x] `hooks/recovery.py`：新增 external_pending polling 补偿 worker，按配置控制开关、间隔、batch size 和失败退避，查询飞书实例状态并同步。
- [x] `hooks/feishu_conversation.py`、`runtime/feishu_approval_overlay.py`：保留 incident thread 回写；自定义审批卡片降级为可选通知/回退，不再作为主审批入口。
- [x] 部署配置和文档：补 `platforms.feishu.approval.*` 配置说明、webhook callback path 和环境变量。
- [x] `tests/test_feishu_native_approval.py`、`tests/test_feishu_approval_event.py`、`tests/test_approval_async.py`、`tests/test_alert_webhook.py`、`tests/test_alert_webhook_server.py`、`tests/test_recovery.py`、`tests/test_feishu_approval_config.py`：按 TDD 先补失败用例，再实现。

## 文件所有权

| 区域 | 负责人 | 文件 | 说明 |
| --- | --- | --- | --- |
| 审批记录与投递编排 | implementation-agent | `toolsets/approval_async.py` | 创建审批、写回 `approval_message_id`、投递/补发编排 |
| 飞书审批卡片 | implementation-agent | `hooks/feishu_conversation.py`, `runtime/feishu_approval_overlay.py` | 卡片 payload、回复线程、按钮回调字段 |
| 告警入口顺序 | implementation-agent | `hooks/alert_webhook.py` | 先绑定 incident，再发审批卡片，复用旧 pending |
| 启动恢复 | implementation-agent | `hooks/recovery.py` | 扫描旧 pending 审批并补发 |
| 回归测试 | test-agent | `tests/test_approval_async.py`, `tests/test_feishu_conversation.py`, `tests/test_alert_webhook.py`, `tests/test_recovery.py`, `tests/test_feishu_approval_overlay.py`, `tests/test_message_delivery.py` | 覆盖投递、补发、顺序、payload、sent outbox 补回写 |
| 原生审批状态机 | implementation-agent | `toolsets/approval_async.py` | 外部审批字段、状态迁移、幂等同步、执行门禁 |
| 飞书原生审批客户端 | implementation-agent | `toolsets/feishu_native_approval.py` | 创建审批实例、错误分类、审批链接返回 |
| 飞书审批事件 webhook | implementation-agent | `hooks/feishu_approval_event.py` | 校验来源、解析事件、同步本地状态 |
| 原生审批告警入口 | implementation-agent | `hooks/alert_webhook.py`, `hooks/feishu_conversation.py` | 创建原生审批、线程通知、卡片降级 |
| webhook 服务入口 | implementation-agent | `hooks/alert_webhook_server.py` | 注册飞书审批 callback path，保持 Alertmanager route 不变 |
| 原生审批补偿 | implementation-agent | `hooks/recovery.py` | external_pending polling、batch、退避 |
| 原生审批回归测试 | test-agent | `tests/test_feishu_native_approval.py`, `tests/test_feishu_approval_event.py`, `tests/test_approval_async.py`, `tests/test_alert_webhook.py`, `tests/test_alert_webhook_server.py`, `tests/test_recovery.py`, `tests/test_feishu_approval_config.py` | 覆盖创建、事件、幂等、执行门禁、补偿、route、配置 |

## 子 Agent 分工

| 任务 | 范围 | 可写文件 | 必须补充的测试 |
| --- | --- | --- | --- |
| 审批创建与写回 | `approval_async` 的 DB 写回和投递协调 | `toolsets/approval_async.py` | `tests/test_approval_async.py` |
| 飞书卡片发送 | approval 卡片内容和线程回复 | `hooks/feishu_conversation.py`, `runtime/feishu_approval_overlay.py` | `tests/test_feishu_conversation.py`, `tests/test_feishu_approval_overlay.py` |
| 告警主流程 | incident 绑定后再发审批卡片、旧 pending 补发 | `hooks/alert_webhook.py`, `hooks/recovery.py` | `tests/test_alert_webhook.py`, `tests/test_recovery.py` |
| 原生审批状态机 | approval 外部字段、`external_pending`、`resolve_external_approval(...)`、执行门禁 | `toolsets/approval_async.py` | `tests/test_approval_async.py` |
| 飞书 OpenAPI 客户端 | 创建审批实例、查询审批实例、错误分类、链接保存 | `toolsets/feishu_native_approval.py` | `tests/test_feishu_native_approval.py` |
| 飞书审批事件 | 审批实例状态变更 webhook，只同步状态不执行 | `hooks/feishu_approval_event.py` | `tests/test_feishu_approval_event.py` |
| 告警入口与通知降级 | 原生审批主路径、自定义卡片降级、thread 通知 | `hooks/alert_webhook.py`, `hooks/feishu_conversation.py`, `runtime/feishu_approval_overlay.py` | `tests/test_alert_webhook.py`, `tests/test_feishu_conversation.py` |
| webhook 服务入口 | Alertmanager webhook 服务同时挂载飞书审批 callback route | `hooks/alert_webhook_server.py` | `tests/test_alert_webhook_server.py` |
| polling 补偿 | external_pending 批量查询、退避、最终同步 | `hooks/recovery.py` | `tests/test_recovery.py` |

## 开发主管上下文保护

- `dev-lead-agent` 启动后先读取 `using-superpowers`，由 Superpowers 自动选择开发流程 skill。
- `dev-lead-agent` 不直接读取源码全文、不直接改代码、不直接跑测试。
- 代码实现交给 `implementation-agent`。
- 测试与验收交给 `test-agent`。
- 独立审查交给 `review-agent`。
- 三个子 agent 只返回摘要，不返回大段源码、完整日志或完整 diff。

## 验证计划

- `python3 -m py_compile toolsets/approval_async.py toolsets/message_delivery.py hooks/feishu_conversation.py hooks/alert_webhook.py hooks/recovery.py runtime/feishu_approval_overlay.py`
- `python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q`
- 2026-05-11 验证结果：`68 passed, 14 warnings`。
- 重点看四件事：新审批返回 `approval_message_id`，旧 pending 可补发，Alertmanager 顺序先 binding 后 approval，sent outbox 能补回写且不重复发卡。
- `python3 -m py_compile toolsets/approval_async.py toolsets/feishu_native_approval.py hooks/feishu_approval_event.py hooks/alert_webhook.py hooks/feishu_conversation.py hooks/recovery.py runtime/feishu_approval_overlay.py`
- `python3 -m pytest tests/test_feishu_native_approval.py tests/test_feishu_approval_event.py tests/test_approval_async.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_feishu_conversation.py -q`
- `python3 -m pytest tests/ -q`
- 重点看五件事：飞书实例创建失败不执行，审批事件幂等同步，终态不能回滚，`external_pending` 不能进入 execution worker，polling 能补偿遗失 webhook。
- 2026-05-15 验证结果：`python3 -m py_compile toolsets/approval_async.py toolsets/feishu_native_approval.py hooks/feishu_approval_event.py hooks/alert_webhook.py hooks/feishu_conversation.py hooks/recovery.py runtime/feishu_approval_overlay.py hooks/alert_webhook_server.py` 通过。
- 2026-05-15 验证结果：`python3 -m pytest tests/test_alert_webhook_server.py tests/test_feishu_approval_config.py tests/test_feishu_approval_event.py -q`，17 passed, 1 warning。
- 2026-05-15 验证结果：`python3 -m pytest tests/test_feishu_native_approval.py tests/test_feishu_approval_event.py tests/test_approval_async.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_feishu_conversation.py tests/test_feishu_approval_config.py tests/test_alert_webhook_server.py -q`，93 passed, 15 warnings。
- 2026-05-15 验证结果：`python3 -m pytest tests/ -q`，317 passed, 15 warnings。

## 交接说明

- 这次修复的可见性门槛是 `approval_message_id`，不是单纯的 approval record。
- 任何 pending 但没有卡片的审批都应视为“未完成投递”，需要补发或进入恢复扫描。
- 2026-05-15 起，主审批入口切换到飞书原生审批；自定义审批卡片只能作为通知展示/回退，不再代表主审批事实。
- 飞书审批结果必须先同步到本地 approval 状态，再由本地 execution worker 根据本地状态和安全门禁执行。
