# ADR-0001：飞书原生审批作为审批主路径

日期：2026-05-15

状态：已接受

## 背景

CR-2026-05-15-001 要求把审批主路径从自定义飞书审批卡片升级为飞书原生审批。本地 approval 创建后进入 `external_pending`，系统创建飞书原生审批实例并保存外部字段；飞书事件或 polling 补偿只负责同步本地状态。只有本地状态变为 `approved` 后，execution worker 才能执行修复动作。

既有 CR-2026-05-11-002 已修复自定义审批卡片的投递与回写问题，这些能力仍可用于 incident thread 通知和显式 fallback，但不再定义默认审批主路径。

## 决策

采用飞书原生审批作为默认人工审批界面：

- `toolsets/feishu_native_approval.py` 直接调用飞书 OpenAPI `POST /approval/v4/instances` 创建审批实例，`uuid` 固定为本地 `approval_id`。
- 生产依赖不使用 `lark-cli`；`lark-cli` 只允许作为人工调试工具。
- `toolsets/approval_async.py` 保存外部审批字段，新增 `external_pending`、`approval_create_failed`，并提供 `resolve_external_approval(...)` 作为唯一外部状态同步入口。
- `hooks/feishu_approval_event.py` 校验飞书事件来源，解析 `uuid`、`instance_code`、`status` 后调用本地同步入口，不直接执行命令。
- polling worker 查询仍处于 `external_pending` 的审批实例，补偿 webhook 丢失或状态同步失败。
- 自定义飞书审批卡片降级为通知或显式 fallback；已有原生审批实例时，卡片不得同时提供可批准动作。

## 约束

- 本地 approval 状态是执行门禁的唯一依据；飞书事件、polling 和 fallback 卡片都不能绕过本地状态机。
- `executed`、`failed`、`denied`、`canceled`、`expired` 等终态不能被外部事件覆盖。
- 飞书 webhook 必须校验 token / 签名 / 时间戳 / app_id 等来源信息，非法或重放请求不得进入状态同步。
- 执行参数只能从本地 approval 记录读取，不能信任飞书事件载荷中的命令或资源字段。
- polling 必须受配置控制，并带 batch size、间隔、指数退避和上限。

## 影响

API / 模块影响：

- 新增 `toolsets/feishu_native_approval.py`。
- 新增 `hooks/feishu_approval_event.py`。
- 扩展 `toolsets/approval_async.py` 的状态机、字段迁移、幂等同步和执行门禁。
- 调整 `hooks/alert_webhook.py`、`hooks/feishu_conversation.py` 的审批入口和 thread 通知语义。
- 扩展 `hooks/recovery.py`，增加 `external_pending` polling 补偿。

数据影响：

- approval 表需要保存 `external_provider`、`external_uuid`、`external_approval_code`、`external_instance_code`、`external_status`、`external_url`、同步时间、polling 退避和错误字段。
- 旧字段 `approval_message_id` 保留，用于旧卡片投递补偿和 fallback。

部署影响：

- 新增 `platforms.feishu.approval.*` 配置，覆盖原生审批开关、审批定义 code、requester open_id、OpenAPI 超时、webhook 校验、polling 和 fallback。
- 部署顺序必须先迁移字段，再配置飞书事件订阅和 polling，最后启用原生审批主路径。

## 备选方案

继续以自定义飞书审批卡片为主路径：

- 优点：改动小，复用已有投递与回写逻辑。
- 缺点：无法使用飞书原生审批实例、审批流和统一审计；仍需维护卡片按钮作为审批权威入口。

通过 `lark-cli` 创建生产审批：

- 优点：短期接入成本低。
- 缺点：CLI 进程、输出格式、认证和错误处理不适合作为生产依赖；会扩大运行时攻击面和故障面。

## 后果

正向后果：

- 审批体验和审计回到飞书原生审批体系。
- 本地执行门禁更清晰，外部事件只同步状态。
- webhook 与 polling 形成最终一致闭环。

代价：

- approval 状态机和数据字段增加。
- 需要新增飞书事件订阅、OpenAPI 凭证、polling worker 配置和监控。
- fallback 卡片必须严格互斥，防止双入口审批冲突。
