# Notification Center 与 Feishu Notification-only 通道

## 边界

Gateway/control-plane 是通知投递、投递记录和重试状态的唯一入口。Feishu 只作为 notification-only channel：消息卡片可以展示摘要和内部 Console 链接，但不能携带 `approve`、`reject`、`approval_decision` 等会改变 approval 状态的交互值。

Approval 状态只允许由内部 Approval Service API 推进。通知失败必须记录为投递失败或 dead-letter，不能反向导致 approval request 创建失败。

## Gateway API

- `POST /notifications/send` / `POST /api/notifications/send`：发送或登记通知，返回 `202`；即使 Feishu 当前失败，只要已落库也返回投递记录。
- `GET /notifications/types` / `GET /api/notifications/types`：返回通知类型与 Feishu 卡片模板样例。
- `GET /notifications/deliveries?status=&notification_type=&limit=` / `GET /api/notifications/deliveries`：查询投递记录。
- `POST /notifications/retry` / `POST /api/notifications/retry`：重试指定 `delivery_id`，或重试所有到期 failed 记录。

## 通知类型

- `new_incident`：Gateway 创建或复用 Incident 后通知。
- `diagnosis_ready`：诊断结果可查看。
- `approval_required`：内部 Approval Center 有待审批项。
- `approval_result`：审批结果已由内部系统产生。
- `execution_result`：执行结果可查看。
- `unowned_alert`：告警未匹配服务归属，发送默认团队。

## 团队渠道选择

配置入口为 `AIOPS_NOTIFICATION_CHANNELS_JSON`：

```json
{
  "default_team_id": "default",
  "teams": {
    "default": {"name": "Default SRE", "feishu_chat_id": "oc_default"},
    "payments": {"name": "Payments", "feishu_chat_id": "oc_payments"}
  },
  "services": {
    "checkout-api": {"team_id": "payments"},
    "billing-api": {"team_id": "payments", "feishu_chat_id": "oc_billing"}
  }
}
```

解析顺序：请求显式 `feishu_chat_id` > service 专属 `feishu_chat_id` > service.owner team > 请求 `team_id` > default team。没有归属的 `unowned_alert` 会进入 default team。

## 模板样例

`approval_required` 卡片样例：

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "template": "orange",
    "title": {"tag": "plain_text", "content": "AIOps 待审批提醒"}
  },
  "elements": [
    {
      "tag": "markdown",
      "content": "有操作建议等待审批。飞书只负责通知，审批必须在内部系统完成。\n**摘要:** 需要审批重启 checkout-api\n**Incident:** inc-1\n**服务:** checkout-api\n**团队:** payments\n**风险:** high\n**审批:** ap-1"
    },
    {
      "tag": "action",
      "actions": [
        {
          "tag": "button",
          "text": {"tag": "plain_text", "content": "打开 Approval Center"},
          "type": "primary",
          "url": "https://console.example.test/approval-center/ap-1"
        }
      ]
    }
  ]
}
```

注意：按钮只有 `url`，没有 `value` 或审批动作字段。按钮 URL 由服务端基于 `AIOPS_CONSOLE_BASE_URL` 和内部对象 ID 生成，请求体中的 `console_url` / `url` 不会被信任或透传到飞书卡片。

## 投递记录样例

```json
{
  "id": "b9b8c0b1-1a0c-45c8-89fb-3ce98f74f9ab",
  "notification_id": "ntf-1",
  "notification_type": "approval_required",
  "incident_id": "inc-1",
  "approval_id": "ap-1",
  "service_id": "checkout-api",
  "team_id": "payments",
  "platform": "feishu",
  "receive_id_type": "chat_id",
  "chat_id": "oc_payments",
  "template_id": "feishu.approval_required.v1",
  "delivery_status": "sent",
  "delivery_attempts": 0,
  "max_attempts": 3,
  "target_message_id": "om_xxx",
  "payload_hash": "sha256...",
  "dedupe_key": "approval_required:inc-1:ap-1",
  "last_delivery_error": null
}
```

状态：`pending`、`sent`、`failed`、`suppressed`、`dead_letter`。

## Hermes Legacy 迁移策略

短期兼容：保留 Hermes 现有告警/审批通知代码，不在本任务强删，避免影响当前 Feishu gateway 路径。

新增路径：所有 Gateway/control-plane 新功能必须调用 Notification Center API，不再调用 `hooks.feishu_conversation.publish_approval_card()` 或 Hermes 侧 direct chat sender。

废弃方向：

- `publish_approval_card()` 属于 legacy interactive approval card，后续应停止调用。
- `approval_reply` 和 Feishu card callback 仍只用于旧路径兼容，不能成为内部 Approval Center 状态源。
- 迁移完成后，Hermes 只输出 diagnosis/action proposal；通知由 Gateway 根据 incident/service/team 归属统一投递。
