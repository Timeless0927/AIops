Type: grilling
Status: resolved
Blocked by: 01 (Establish Current Pilot Baseline)

# Define Web Setup And Integration Ownership

## Question

What state, security boundary, and lifecycle should the optional resumable Web setup flow use for Platform Administrator-managed model Provider, Notification Destination, Connector Enrollment, Cluster registration, and dependency verification?

Resolve secret ownership and masking, connection tests, credential rotation, skipped-step semantics, readiness state, authorization and fresh-auth requirements, and what remains usable when a dependency is absent. Deployment environment variables must not remain the normal product configuration path for model or notification providers.

## Answer

Web Setup 是 Platform Administrator 可跳过、可恢复的配置工作流，不是独立领域状态机。它不持久化 step number 或全局 `setup_completed`；Console 每次从真实 owner state 和 verification record 构造流程，Platform Status 持续显示同一组事实。配置被修改、删除、停用或失去依赖时，状态自然降级，不保留过期的“已完成”结论。

### State ownership

| State | Owner | Durable store |
| --- | --- | --- |
| 单一 Model Provider configuration、encrypted API key、revision 与 verification | Diagnosis | `diagnosis.db` |
| Notification Destination/Route、encrypted provider credential 与 verification | Notification Engine | `notification.db` |
| Connector Enrollment、credential hash、Cluster registration/heartbeat 与 read verification | Gateway | `gateway.db` |
| Platform-level setup skip decision 与 admin governance audit | Gateway | `gateway.db` |
| Platform Status | Gateway 聚合 owner response | 不复制 integration state |

Gateway 是唯一 browser API Adapter，负责 Session、Platform Administrator authorization、CSRF、fresh-auth、reason、masked audit 和 request correlation。它不持久化 Model 或 Notification plaintext/config 副本。Diagnosis 和 Notification Engine 只接受 Gateway Service Identity 的内部管理请求，并各自在所属数据库中原子持久化 configuration revision 和 operation result。

Platform Status 对每项 capability 分开表达：

```text
configuration: absent | present
setup_decision: active | skipped
verification: not_applicable | unverified | verifying | verified | failed | stale
availability: available | degraded | unavailable
```

Connector/Cluster 另外投影 `pending_registration`、`online`、`offline` 和 `rotation_pending` 等真实 connection state。Verification 必须绑定 exact configuration revision，并记录 `checked_at` 与安全 result code；配置或 credential 变化立即使旧 verification stale。`05` 再定义 provider timeout/error taxonomy 和 verification freshness window。

Skip 是平台级决定，记录 actor、reason 和 time，不是每个 User 的浏览器偏好。它只对尚未 ready 的 capability 可用；verified/online 后的停用必须走该 owner 已有的 disable/delete/revoke contract，不能把 skip 变成第二套运行开关。测试失败的 encrypted configuration 可以在 skipped 状态下保留以便恢复；保存新配置自动把 `setup_decision` 改回 `active`。

### Model Provider

Pilot 只有一个当前 OpenAI-compatible Model Provider，不增加 provider registry、优先级或自动 failover。Diagnosis 使用只挂载给自身的 `aiops-model-encryption` Secret 加密 API key；endpoint、model、timeout、revision 和 ciphertext 存于 `diagnosis.db`。`AIOPS_MODEL_*` 环境变量退出正常产品配置和 deployment fallback，只允许定向测试使用。

保存配置先产生 `present/unverified` revision，不要求同步测试成功。Diagnosis 从自己的 Pod network 使用已保存配置执行最小真实 model request；只有 exact verified revision 可以启动新的 Diagnosis Job。配置或 credential 更新原子替换当前 ciphertext、删除旧 ciphertext 并阻止新 Job，直到新 revision 验证成功；不回退到旧 Provider、keyword diagnosis 或其他模型。

每个已开始的 Diagnosis Job 冻结 `provider_revision` 并只在进程内持有该 revision 的解密配置。配置更新后已有 Job 可以完成，但不得切换 revision；若 Diagnosis 在完成前重启而旧 ciphertext 已删除，Job 以 `provider_revision_unavailable` 失败，不自动重跑。

### Notification Destination

Notification credential 继续由 Notification Engine 使用 `aiops-notification-encryption` 加密。Destination 先保存为 unverified，再由 Engine 使用真实 provider path 发送清晰标记的测试消息。测试成功本身不改变 Route；Platform Administrator 还必须显式选择“用于 Pilot 通知”，Engine 才创建或更新一条指向该 verified Destination 的普通 catch-all Route。已有更高优先级 Route 继续优先，immutable final suppress Route 继续兜底。

Destination credential 变化立即使 verification stale，并暂停该 Destination 的 pending Delivery；暂停不消耗 attempt，也不进入 dead-letter。重新验证成功后，Delivery 使用新 credential 和原有 frozen presentation 继续。删除前必须禁用关联 Route，存在 unfinished Delivery 时只允许 disable。系统不回退旧 credential、不自动改投其他 Destination。

### Connector Enrollment and Cluster registration

Gateway 从现在起允许多个唯一 `connector_id`/`cluster_id` Enrollment 并行连接同一个 Gateway，不得存在 singleton Cluster state。Pilot acceptance 仍只验证一个 Cluster，本次不发布 connector-only bundle；未来远程 Connector packaging 不改变 Enrollment、registration 或 status contract。

Platform Administrator 在 Web 指定 unique `connector_id` 与 `cluster_id` 并创建 Enrollment。Gateway 只显示 plaintext credential 一次并仅持久化 hash。Platform Operator 把以下三项写入目标 Cluster 的 `aiops-connector-secret` 并重启 Connector：

```text
AIOPS_CONNECTOR_ID
AIOPS_CLUSTER_ID
AIOPS_CONNECTOR_CREDENTIAL
```

Gateway 不获得 Kubernetes Secret write 权限。与 Gateway 同 Cluster 的 Connector 使用 ClusterIP HTTP；其他 Cluster 必须使用验证证书的 HTTPS `/connectors` endpoint，不能用 HTTP NodePort 传输 credential、heartbeat 或 command。

Registration 和 heartbeat 只证明 identity 与 connectivity，不能标记 verified。Gateway 还必须执行一个 bounded read verification command，确认目标 Cluster identity、API discovery 和 permission summary。相同 pair 重注册幂等；任一 ID 已绑定另一 Enrollment 时拒绝。Heartbeat offline 保留历史 verification，但 readiness 降级并停止 live evidence、dry-run、Execution Grant 和 mutation dispatch。

Credential rotation 使用 staged handoff：Enrollment 先进入 `rotation_pending` 并停止新 grant/command；已有 command 达到可信 terminal，且不存在 started mutation、Unknown Outcome 或 unfinished rollback 后，Gateway 才生成并一次性显示 15 分钟 candidate credential。旧 credential 保持有效；candidate 首次完成 exact pair registration/heartbeat 后原子替换 current hash。candidate 未使用则过期且旧 credential 保持有效。安全事件可以立即 disable Enrollment，但运行中 mutation 进入既有 Unknown Outcome reconciliation。

### Security and lifecycle

只有 Platform Administrator 能读取 masked integration detail、配置、测试、轮换、skip 或恢复。所有已认证 User 可读取 capability-level Platform Status，包括 configuration、verification、availability、last checked 和安全 reason code；普通 SRE 看不到 endpoint、provider account、recipient、Enrollment credential、skip reason 原文或 secret metadata。未登录用户只能读取普通 health endpoint。

创建、修改、删除、轮换或测试 Model Provider/Notification Destination，以及创建或轮换 Connector Enrollment，都要求 5 分钟内 fresh-auth。Masked read 和 skip/unskip 不要求 fresh-auth。所有写入仍要求 CSRF、reason 和 audit。

API 永不返回 plaintext、ciphertext、hash、secret suffix 或可重放 masked placeholder，只返回 `*_configured`。Update 省略 secret 字段表示保留；新的非空值表示 rotation；`***` 等 placeholder 永不保存。启用配置不能清空必需 credential，删除必须走显式 owner lifecycle。所有共享配置写入带 `expected_revision`，revision 漂移返回 `stale_configuration`；同一 Enrollment 同时最多一个 pending rotation。

Owner mutation 以 `request_id` 幂等。Gateway 先记录 admin intent，owner 在配置 transaction 中记录 revision 与 masked result，Gateway 再记录 `succeeded` 或 `rejected`。响应丢失时记录 `outcome_unknown`，按 `request_id` 向 owner 对账；对账前禁止重复 credential mutation。

External Model endpoint 只允许 HTTPS，并拒绝 userinfo、loopback、link-local、private/cluster address、metadata endpoint、Kubernetes API 和跨-origin credential redirect。`cluster_internal` Model endpoint 可以显式使用 `.svc`/private HTTP 或 HTTPS，但仍拒绝 loopback、link-local、metadata endpoint 和 Kubernetes API。DNS resolution 与 redirect target 每次重验，Authorization 不跨 origin。

Feishu/DingTalk webhook 只允许 external HTTPS。SMTP 显式选择 `external` 或 `cluster_internal`，两者都强制 STARTTLS/TLS，并应用同类 address 和 DNS rebinding guard。

Verification 只保留每个 configuration revision 的最后结果：revision、time、actor、result code、latency 和安全 provider/destination summary。测试 prompt/response、Authorization header、webhook body、SMTP transcript 和 secret 不持久化；immutable admin audit 仍记录每次 test action/outcome，Notification test 复用脱敏 Delivery attempt。

### Resumability and degradation

首次登录不强制跳转或阻塞 Incident workspace。Console 提供明显但可关闭的 setup/Platform Status 入口，并始终允许从导航重新进入；具体布局和 responsive interaction 由 `07` prototype 决定。

每个 owner 从真实消费路径执行 verification，失败保留 configuration 和结果以便修改或重试。Gateway 实时、限时并行聚合 owner masked status，不复制配置；单个 owner timeout 只使该 capability `unavailable`，Platform Status 仍返回其他部分。

缺失依赖只阻塞直接能力：

- Model/Diagnosis 不可用时 Alert 仍创建 Incident，Diagnosis Request 保持明确 pending/blocked；管理、已有 Incident、Human Input 和 Report history 可用。
- Notification 不可用时业务 transaction 照常提交，Notification Request 保持 pending 或明确 suppressed，不回滚 Incident、Approval 或 Execution。
- Connector/Cluster offline 时已有数据可读，但停止 live evidence、server-side dry-run、Execution Grant 和新 mutation dispatch。
- 恢复后从 durable state 继续，不生成 fake success、keyword diagnosis 或 automatic mutation retry。
