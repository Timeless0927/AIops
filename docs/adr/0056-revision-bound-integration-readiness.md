# Integration readiness 绑定 verification revision 与 live availability

Status: accepted

Model 与 Notification capability 不使用永久 setup completion 或单一健康布尔值。Gateway 从 owner-held exact configuration revision、verification 和最近真实调用的 availability 计算每项 readiness；verification 不按时间自动过期，配置变化使其 stale，确定性 credential/config rejection 使其 failed，瞬时故障只降级 availability。完整 Pilot 验收另要求 15 分钟内的新 verification。

Model verification 必须通过真实 OpenAI-compatible Adapter 完成 tool call 和结构化 JSON 两轮 probe；运行中 Model failure 明确终结 Diagnosis Job，不得回退为 keyword diagnosis 或自动重跑。Notification verification 复用 durable Delivery，只有真实 Provider response 形成 terminal `sent` 才成功；普通 Delivery 继续 bounded retry/dead-letter，Destination hard failure 暂停其他 pending Delivery。两类 test 都异步持久化并返回 `202`，避免让 browser request 承担 Provider latency 或另建 setup state machine。

Web Setup 的 skip 只降级直接能力，不能伪装 ready。它允许日常产品能力继续使用，但不能替代 Pilot acceptance 中的真实 Model probe、selected Notification Destination test delivery 和人工收件确认。
