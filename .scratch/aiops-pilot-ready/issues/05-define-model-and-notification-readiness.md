Type: grilling
Status: resolved
Blocked by: 03 (Define Web Setup And Integration Ownership)

# Define Model And Notification Readiness

## Question

What public contracts and visible states prove that a Web-configured OpenAI-compatible model Provider and one selected Notification Destination are genuinely ready for Pilot use, and how should timeout, authentication failure, rate limiting, unavailable providers, and configuration changes degrade without false success?

The model path must not silently present keyword fallback as model diagnosis. Notification setup may be skipped, but a completed test delivery is required before that capability is marked verified.

## Answer

Model 与 Notification readiness 不是一次性 setup completion。每项 capability 分别由 exact configuration revision 的 verification、最近真实调用形成的 availability，以及 Gateway 计算但不持久化的 readiness 共同表达。`verified` 不因时间流逝自动过期；配置或 credential 变化使旧 verification `stale`，确定性 credential/config rejection 使其 `failed`，瞬时运行故障只改变 availability。Pilot 验收另要求使用验收开始前 15 分钟内完成的 verification。

### Public status contract

所有 authenticated User 通过 `GET /api/v1/platform/status` 读取以下公共结构；Gateway 实时聚合 owner response，不复制 integration state，也不返回全局 `all_ready`。本票固定 `model` 与 `notification` 两项，后续 capability 使用相同 `CapabilityStatus` shape：

```text
PlatformStatus {
  generated_at: timestamp
  capabilities: {
    model: CapabilityStatus
    notification: CapabilityStatus
  }
}

CapabilityStatus
```

```json
{
  "readiness": "ready | not_ready | skipped",
  "configuration": "absent | present",
  "configuration_revision": "opaque-or-null",
  "setup_decision": "active | skipped",
  "verification": {
    "operation_id": "opaque-or-null",
    "state": "not_applicable | unverified | verifying | verified | failed | stale",
    "revision": "opaque-or-null",
    "checked_at": "timestamp-or-null",
    "reason_code": "code-or-null"
  },
  "availability": {
    "state": "available | degraded | unavailable",
    "observed_at": "timestamp-or-null",
    "reason_code": "code-or-null"
  }
}
```

Model readiness 只在 configuration `present`、setup decision `active`、exact revision `verified` 且 availability `available` 时为 `ready`。Notification 还要求一个 verified Destination 已被明确选为 Pilot catch-all Route；测试成功本身不改 Route。`skipped` 永远不能投影为 `ready`，但只阻塞直接依赖 capability。

普通 User 看不到 Provider endpoint/account、model account、recipient 或错误原文。Platform Administrator 的 owner detail 仍只返回 masked configuration。Public revision 是不可猜测且不敏感的 opaque identity，只能作为匹配当前 revision 的 `expected_revision` 原样回传，不包含 secret suffix 或 hash。

公开 `reason_code` 固定为：

```text
not_configured
test_required
configuration_changed
authentication_failed
rate_limited
timeout
provider_unavailable
provider_rejected
invalid_response
owner_unavailable
```

Provider 原始错误只进入脱敏 operational log，不进入 Console response、verification record 或 governance audit。Gateway 聚合单个 owner 超过 3 秒时只把该 capability 标记为 `unavailable/owner_unavailable`，其他 capability 继续返回。

### Model verification

Platform Administrator 通过 `POST /api/v1/admin/model-provider/test` 为当前 exact revision 创建 durable asynchronous verification；API 在 Diagnosis 持久化 `verification_id`、revision 和一次性 nonce 后返回 `202`。`verification_id` 同时作为 Platform Status 和 masked Model Provider detail 中的 `verification.operation_id` 返回，state、checked time、reason code 与 terminal result 始终关联该 identity。Diagnosis worker 使用配置的真实 `/chat/completions` Adapter 完成一个不含 Incident、Cluster 或 User 数据的两轮 probe：

1. 第一轮必须产生合法的指定 `readiness_probe` tool call。
2. Diagnosis 注入一次性 nonce 的 tool result；第二轮必须返回可解析 JSON 并原样包含 nonce。

普通文本、缺失 tool call、错误 tool arguments、无法解析的 JSON 或 nonce 不匹配都以 `invalid_response` 失败。该 probe 同时证明 endpoint、credential、model、tool calling 和结构化输出可用；只有 exact revision 成功才写 `verified`。Verification 只持久化 revision、time、actor、安全 result code、两轮总 latency 和安全 provider summary，不保留 prompt、response、tool payload、Authorization header 或 token usage detail。

Verification worker 重启后可重领同一 record 并重复 harmless probe；允许少量重复模型费用，不创建产品状态或 mutation。Model 每轮 request timeout 默认 30 秒，Web 允许配置 5 至 120 秒并计入 revision；verification 使用 exact configured timeout。

新的 Diagnosis Request 只有在 Model capability `ready` 时才能开始 Diagnosis Job。每个已开始 Job 冻结 `provider_revision`。任一 Model 调用失败时，当前 Job 以对应 reason code 明确 `failed`，保留已采集 Evidence，并在 Investigation 显示模型诊断失败；不得生成、包装或展示 keyword diagnosis，也不得自动重跑 Job。Provider 恢复并重新测试成功后，由 User 显式创建新的 Investigation/retry；尚未开始的 Request 保持 `blocked`。

一次 `timeout` 或 `rate_limited` 立即使 availability `degraded`；`provider_unavailable` 或 `invalid_response` 立即使其 `unavailable`，但保留 exact revision 的历史 `verified`。`authentication_failed` 或确定性 `provider_rejected` 同时使 verification `failed` 和 availability `unavailable`。下一次真实 verification 成功恢复 `available`；不使用连续失败阈值或后台自动 health probe。

### Notification verification and delivery

Destination test 复用 Notification Engine 的普通 durable Delivery，而不在 HTTP request 中同步等待 Provider，也不建立第二套测试状态机。现有 Destination test API 返回 `202` 和 `delivery_id`，该 identity 同时作为 `verification.operation_id` 返回，verification 进入 `verifying`；Engine 使用 exact Destination revision 和真实 Apprise Provider Adapter 发送带明显 test 标记、test ID 与 time 的消息。最多 3 次 attempt，指数退避基数 2 秒；`Retry-After` 优先但单次最多 300 秒。每次 Provider attempt 固定 timeout 10 秒，现有 Delivery query 对未完成 attempt 返回 `next_attempt_at`，使 Console 能显示何时继续验证。

只有 terminal `sent` Delivery 才使 exact Destination revision `verified`；`dead_letter` 使 verification `failed`。Provider 成功 response 是产品内可验证的完成边界，不要求管理员另建“已收到”状态，因为 Feishu、DingTalk 与 SMTP 没有统一端到端 read receipt。完整 Pilot 验收仍要求人员实际看到该 test message。

普通 Delivery 的 `timeout`、`rate_limited` 和 `provider_unavailable` 按相同 bounded retry contract 运行，并立即把 Destination availability 降为 `degraded`，但不撤销历史 `verified`；后续成功 Delivery 自动恢复 `available`。`authentication_failed` 或确定性 `provider_rejected` 使 verification `failed`、availability `unavailable`，并暂停该 Destination 的其他 pending Delivery，暂停不消耗 attempt。单条消息的 `invalid_response` 只影响该 Delivery 的 retry/dead-letter，不撤销 Destination verification。修复配置并完成新 test 后恢复 pending Delivery，仍使用原 frozen presentation。

Destination revision 变化立即使 verification `stale`，并按 03 的 owner lifecycle 暂停 pending Delivery；系统不回退旧 credential、不自动切换 Destination。删除仍要求先禁用关联 Route，unfinished Delivery 存在时只能 disable。

### Public management and acceptance

Gateway 公开的最小 browser surface 是：

```text
GET /api/v1/platform/status
GET|PUT|DELETE /api/v1/admin/model-provider
POST /api/v1/admin/model-provider/test
/api/v1/admin/notification-destinations* existing management/test paths
existing Notification Delivery query paths
```

Test API 均返回 `202`；Console 以 response identity 轮询 owner detail、Platform Status 或现有 Delivery query，不新增 WebSocket/SSE。所有 integration mutation 继续要求 Platform Administrator、CSRF、5 分钟 fresh-auth、reason、`request_id` idempotency 和 `expected_revision`。Test result 必须能按 verification/delivery identity 关联 masked admin audit。

Notification setup 可以为 `skipped`，且不会阻塞 Incident、Diagnosis、Approval、Execution 或 Report；Notification Request 继续 durable pending 或明确 suppressed，不回滚业务 transaction。但完整 Pilot-ready Release 验收不能用 skip 代替真实通知：验收时必须同时证明 Model exact revision 在 15 分钟窗口内通过两轮 probe、Platform Status 的 Model capability 为 `ready`、一个 selected Notification Destination 在同一窗口内产生 terminal `sent` test Delivery、Platform Status 的 Notification capability 为 `ready`，且验收人员实际收到测试消息。
