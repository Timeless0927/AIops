# Tickets：AIOps 架构加深

按 [AIOps 架构加深规格](./PRD.md) 以行为保持的责任迁移加深五处 Module，并删除旧 owner。

Work the **frontier**：任何 blockers 已全部完成的票都可以开始；有重叠 Gateway 文件的票按依赖串行执行。

## A01 建立 Gateway Chat 请求 Seam

**What to build:** User 的 Chat Session、附件、消息分支和 Investigation Handoff 行为保持不变，同时 Gateway Handler 只通过一个窄 Interface 分发 Chat 请求，不再拥有 Chat 的完整依赖图。

**Blocked by:** None — can start immediately.

- [x] 将 `1bb2d44` 中混入的 Gateway 草稿整理为独立、可评审的行为保持 diff，不重写或回退无关 Console 历史。
- [x] Gateway Handler 对 Chat 只调用 `dispatch(handler, route_path)` 等价的窄 Interface，不传递会话、附件、registry、catalog、incident 和 auth callable 参数簇。
- [x] Chat HTTP Adapter 持有完成路由所需的真实依赖，领域决策仍归属 Chat Session、Attachment、Handoff 和 scope owner。
- [x] Attachment scanner 等 I/O 依赖继续由装配层传入，测试替身不依赖隐式全局创建。
- [x] Chat、Attachment、Handoff 和 Gateway retirement 定向 contracts 通过；既有 migration 53/55 测试债务单独记录。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Chat 请求归属 `ChatHTTPAdapter` Module，公开 Interface 为 `dispatch(handler, route_path)`；超大入口 `main.py` 仅保留零参数装配与路由分发，完成时 809 行，未高于票开始时。定向 selector 为 `test_gateway_http_boundaries.py`、三个 Gateway Chat HTTP contract 与 `test_gateway_v1_retirement.py`；固定评审基准为 `e5506eb`，Standards 与 Spec 均通过。`test_gateway_v1_auth_contract.py` 对 migration 53/55 的既有预期差异仍单独保留，不归因于 A01。

## A02 迁移其余 Gateway 请求 Adapter

**What to build:** User 和 Platform Administrator 的现有 V1 请求行为保持不变，同时 Gateway Handler 不再拥有 Resource、Platform、Integration、Change、Incident 和 Report 的内部依赖图。

**Blocked by:** A01 建立 Gateway Chat 请求 Seam.

- [x] Resource Catalog、Platform Status、Model Provider、MCP、Skill、Notification Admin、Secure Input、Change、Incident 和 Report 路由通过所属能力的窄 Interface 分发。
- [x] Handler 只负责 method/path 分发、输入边界、鉴权调用和响应写出，不新增领域决策或外部调用编排。
- [x] 能力依赖由 Gateway 装配 Module 持有；HTTP Adapter 不继续暴露内部 callable 参数簇。
- [x] 迁移完成后删除旧入口装配代码，不保留 parallel dispatcher、wrapper 或兼容分支。
- [x] Gateway 入口文件不超过票开始时体量，且所有受影响 V1 HTTP contracts、retirement selector 和静态编译通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：其余 13 个 Gateway 能力请求均归属各自 `*HTTPAdapter`，公开 Interface 统一为 `dispatch(handler, route_path)`；`GatewayHandler` 只按既有顺序短路分发，依赖由零参数 `_request_http_adapters()` 装配，旧 module-level dispatcher 已删除。`main.py` 由票开始时 809 行降至 795 行。定向 Gateway V1 contract、retirement 与 Notification 直接调用 selector 共 21 项通过，Gateway 与受影响测试静态编译、`git diff --check` 通过；扩大测试的 14 个失败已在固定基准 `94d6492` 复现，属于既有 migration、fixture 与 Connector heartbeat 债务。固定基准 `94d6492` 上 Standards 与 Spec 均无阻塞项；Standards 仅记录 13 个 Adapter 局部变量样板重复的低优先级判断项，不扩大本票范围处理。

## A03 收敛 Session、Identity Administration 与 Audit owner

**What to build:** User 的会话和 Platform Administrator 的 User、Team、Role 与 audit 管理行为保持不变，同时这些能力由各自 Module 管理，不再共享 Gateway 总 Store Interface。

**Blocked by:** A02 迁移其余 Gateway 请求 Adapter.

- [x] Session Module 只暴露 issue、lookup、freshness、revoke 和 actor projection 所需的最小 Interface。
- [x] Identity Administration Module 拥有 User、Team、Team Membership 和 Role Binding mutation 及其不变量，不隐式创建 Identity Adapter。
- [x] Audit Module 拥有 denial、mutation result 和 query 记录规则，并与业务 mutation 共享 Gateway-owned transaction。
- [x] 迁移后从总 Store 删除旧实现，不保留 wrapper、双写、双读或第二套 admin owner。
- [x] Auth、Admin、Session invalidation、last administrator 和 audit rollback 的 Module 与 HTTP contracts 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Session、Identity Administration 与 Audit 分别归属 `GatewaySessions`、`IdentityAdministration` 与 `GatewayAudit` Module；公开 Interface 分别为 issue/lookup/freshness/revoke/actor projection、identity state/mutation/user-active fact，以及 denial/mutation/query audit。`IdentityHTTPAdapter.dispatch(handler, route_path)` 统一持有 Auth/Admin/Audit HTTP 边界，业务 mutation 与 audit 使用同一个 Gateway-owned transaction。`GatewayV1Store` 由 682 行降至 37 行，只保留 Gateway Database 与待 A04 迁移的 Connector Enrollment 装配；`main.py` 由 795 行降至 596 行，旧 Session/Admin/Audit owner 已删除。

500+ 行文件确认：`main.py` 属于进程装配与请求分发，公开入口为 `GatewayHandler`；`v1_store.py` 属于过渡 Database/Enrollment 装配，公开 Interface 为 `database` 与 `connector_enrollments`。其余被触碰的超大生产文件仅把总 Store 依赖改为既有 `GatewayDatabase` 或 `user_active_in` callable，所属 Module 与公开 Interface 不变：Connector Commands/Enrollment、Kubernetes Change Execution/Reconciliation、MCP Registry、Platform Status、Resource Catalog 与 Skill Registry。定向 selector 为 `test_gateway_identity_owners.py`、`test_gateway_v1_auth_contract.py`、`test_gateway_v1_admin_contract.py`、`test_gateway_http_boundaries.py`，以及这些直接消费者对应的 Connector、MCP、Skill、Secure Input、Incident、Investigation、Platform 与 Kubernetes 测试文件；被修改的 500+ 行测试文件本身即为可独立运行 selector。

主线程复验为核心 9 passed，auth 唯一失败是规格已记录的 migration 53/55 基准债务；直接消费者逐文件 48 passed，Secure Input 的 6 个 migration-order 失败在 `dc791bf` 原样复现。静态编译与 `git diff --check` 通过；固定评审基准为 `dc791bf`，Spec 通过，Standards 首选代理连续两次断流后按规则使用 `fallback`，补齐本完成记录后无剩余代码 finding。

## A04 收敛 Connector Enrollment owner

**What to build:** Platform Administrator 与 Connector 的 enrollment、credential rotation、registration、heartbeat 和 verification 行为保持不变，同时 Connector Enrollment 由独立 Module 管理。

**Blocked by:** A03 收敛 Session、Identity Administration 与 Audit owner.

- [x] Connector Enrollment Module 拥有 enrollment、credential、Cluster presence、registration、heartbeat 和 read verification 不变量。
- [x] Gateway 数据库只共享连接、migration 和 transaction；其他 Module 不通过总 Store 获取 Connector Enrollment 业务状态。
- [x] Command owner 只通过明确 Interface 使用 enrollment facts，不共享 enrollment SQL 或内部表结构。
- [x] 新 owner 可用的同一变更中删除旧实现，不保留 compatibility facade 或双 owner。
- [x] Connector Admin、register/heartbeat、credential rotation、verification 和 restart 直接 contracts 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Connector Enrollment、credential rotation、Cluster presence、registration、heartbeat 与 read verification 归属 `ConnectorEnrollments` Module；公开 Interface 包括 admin/public/readiness state、create/update/register/heartbeat、Cluster administration、verification result，以及 transaction 内 availability、lease identity、verification command facts。`ConnectorEnrollmentHTTPAdapter.dispatch(handler, route_path)` 接管 Connector status、Admin、register 与 heartbeat HTTP 边界；`ConnectorCommands` 只消费装配层注入的 facts/callable，并把 rotation blocker 查询收回 Command owner。旧 `GatewayV1Store`、`v1_store.py`、`_GATEWAY`、入口 Connector handlers、`dispatch_get` 与浅 `admin_state` wrapper 已删除；Gateway 只共享 `GatewayDatabase` 连接、migration 与 transaction。

500+ 行文件确认：`connector_enrollments.py` 属于 Connector Enrollment Module，公开 Interface 如上，由 778 行增至 799 行；`connector_commands.py` 属于 Connector Command Module，公开 Interface 为 queue/poll/start/result/query 与窄 transaction facts，由 524 行增至 559 行；`main.py` 属于进程装配与路由分发，由 597 行降至 395 行。被修改的 500+ 行测试文件本身即为可独立 selector：`test_gateway_diagnosis_delivery.py` 756 行、`test_gateway_kubernetes_change_executions.py` 792 行、`test_gateway_kubernetes_phase_approvals.py` 800 行、`test_gateway_kubernetes_plan_execution.py` 696 行、`test_gateway_v1_change_requests_contract.py` 508 行、`test_gateway_v1_kubernetes_phase_approvals_contract.py` 528 行与 `test_platform_status.py` 752 行；任务开始时恰好 800 行的 Phase Approval 测试完成时未增长。

红绿证据包括 Adapter architecture seam 1 failed→1 passed、V1 Connector `_GATEWAY` 残留 1 failed→2 passed，以及 verification Command 并发替换从错误暴露 `pending_read_commands=1` 到显式只读 transaction 固定同一 snapshot 后通过。主线程聚焦复验覆盖 Connector Commands、Enrollment、HTTP boundary 与 V1 Connector contract；实施者扩大验证为聚焦 19 passed、直接消费者 84 passed、registration/restart 8 passed、Kubernetes execution 3 passed、Change Executions 18 passed、Plan 15 passed、migration 3 passed 与 Platform Status 16 passed，Gateway 源码/测试静态编译和 `git diff --check` 通过。扩大 selector 的 Change Validation 8、Observability 1、Secure Input 6、Phase Approval 12 与 Resource Catalog 1 个失败均在固定基准 `631cb78` 原样复现，分别属于 migration-order、旧 heartbeat fixture 与旧 audit projection 债务。

固定评审基准为 `631cb78`。Spec 首轮发现 verification IDs 与 Command 查询不在同一 snapshot 的并发 blocker，补充公开 `summarize_clusters` seam 回归并修复后复审通过。Standards 首选 reviewer 连续两次流断开后按仓库路由使用 `fallback`，最终无 blocker；旧 owner、跨 owner SQL、schema、API 与 legacy contract 均无残留变更。

## A05 加深 Console Chat 会话 Controller

**What to build:** User 的 Chat 页面行为保持不变，同时视图通过一个会话 controller Interface 使用 SSE、query cache、取消、分支和 Attachment 生命周期。

**Blocked by:** None — can start immediately.

- [x] Chat Session controller 拥有 query/mutation 协调、SSE 事件应用、AbortController、取消、retry/edit/reload 和 Attachment 生命周期规则。
- [x] Chat 视图只拥有展示、输入和 transient UI state，不建立浏览器端的第二套后端状态机。
- [x] server state、URL state 与 transient UI state 的 owner 清晰，页面不再暴露大量跨生命周期回调。
- [x] 迁移使用现有 assistant-ui、TanStack Query 和 shadcn/ui，不增加 facade、Context、状态库或第二套 UI library。
- [x] 旧页面逻辑在新 controller 可用的同一变更中删除，不保留双事件流或兼容 mode。
- [x] Chat controller/page Vitest、TypeScript no-emit、production build 和现有 Chat Playwright selectors 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Chat Session 生命周期归属 `useChatSessionController`，公开 Interface 集中 query/mutation、SSE event application、Abort/cancel、branch、retry/edit/reload 与 Attachment reserve/upload/retry/remove 规则；`ChatPage` 只保留 URL 导航、展示、输入和 transient UI state。旧页面异步 owner、重复 Attachment error owner 与双事件处理已删除，未增加依赖、Context、facade、状态库或 UI library。`chat-page.tsx` 属于 Chat 视图 Module，公开入口为 `ChatPage`，由票开始时 564 行降至 456 行；定向 selector 为 `chat-session-controller.test.ts`、`chat-page.test.tsx`、现有 Chat runtime/client tests 与四个 Chat Playwright specs。

主线程复验为 20 个 Chat Vitest、TypeScript no-emit/production build 与 16 个桌面/移动端 Playwright 全通过，`git diff --check` 通过。固定评审基准为 `dc791bf`；首选评审代理连续两次断流后按规则使用 `fallback`，Spec 无 finding，Standards 仅记录自制 hook runtime 依赖 hook 顺序且不覆盖真实 SSE effect 的低优先级判断项；现有页面行为、Playwright 与 controller seam 已覆盖本票验收，该判断项不阻塞提交，也不在本迁移中引入额外测试框架。

## A06 建立 Clean Acceptance Ledger

**What to build:** Release Owner 看到的 Clean Acceptance 结果保持不变，同时 frontier、attempt、resume、hash、attestation 和 evaluator correction 不变量由一个 ledger Module 管理。

**Blocked by:** None — can start immediately.

- [x] Ledger Interface 接受 typed Gate Result，并集中维护 frontier、attempt、resume、hash、attestation、eligibility 和 correction 不变量。
- [x] Gate Runner、HTTP、Kubernetes、Notification、artifact 和 signing I/O 保留在 Adapter，不进入 ledger 领域决策。
- [x] 本票涉及的完整 ledger 能力从超大 Evidence owner 迁出并删除旧实现；超大文件总行数不得高于票开始时。
- [x] process interruption、safe continuation、no-I/O correction、failed gate 和 seal 行为保持现有 contract。
- [x] Ledger Module tests 不访问真实外部服务；直接 conductor、notification gate、connector gate 和 acceptance contract selectors 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Clean Acceptance ledger 归属 `AcceptanceLedger` Module，公开 Interface 为 create/open、frontier/status/attempt、gate begin/resume/reconcile/record、artifact fact、attestation、eligibility、Promotion Decision 与 seal；所有 `record_gate` consumer 使用 frozen typed `GateResult`。Filesystem/artifact/hash persistence 归属现有 `EvidenceAdapter`，外部 HTTP、Kubernetes、Notification、artifact materialization 与 signing 仍留在各 Gate/Adapter。旧 `AcceptanceEvidence` owner 与重复 promotion/validation 实现已删除，不保留 wrapper 或双 owner。

500+ 行文件确认：原 `evidence.py` 属于 Evidence filesystem Adapter，公开 Interface 为 evidence create/open/persist/artifact/attestation/seal I/O，由 810 行降至 464 行；新 `ledger.py` 属于纯 ledger invariants，公开 Interface 即 `AcceptanceLedger`，完成时 736 行。其他 500+ 行 Acceptance Runner/Gate 文件仍归各自既有 Adapter 或 workflow Module，公开 Interface 不变，本票只把参数类型与 `record_gate` 调用迁到 `AcceptanceLedger`/`GateResult`；`deployment_continuation.py` 的 800→803 行仅来自 type-only owner 标注，不承载新业务能力。被修改的 500+ 行测试文件本身即为可独立运行 selector。定向 selector 为 Ledger、Evidence、Evidence concurrency、Conductor、Evaluator Correction、Promotion、Integrations、Deployment Continuation、DAG、Gate Reuse、Recovery、Recovery Report、Rerun 与 First Run 测试文件。

主线程核心复验为 46 passed；扩大直接消费者为 127 passed，55 个过期 qualification/artifact fixture 失败与 `dc791bf` 同组完全一致。Spec 首轮发现 orphan checksum、artifact I/O 与 stale attempt 三项 blocker，`heavy_worker` 在公开 Seam 完成 3 failed→3 passed 红绿修复，Spec 复审通过。静态编译与 `git diff --check` 通过；固定评审基准为 `dc791bf`，Standards 首选代理连续两次断流后按规则使用 `fallback`，补齐本体量记录后无剩余代码 finding。

## A07 收紧 Notification Request handoff 生命周期

**What to build:** Incident、Investigation 和 Change 的通知行为保持不变，同时领域 Module 通过 typed Notification Request Interface 完成 durable handoff，retry 与 delivery 规则由真实 owner 管理。

**Blocked by:** None — can start immediately.

- [x] 领域 Module 只构造 typed Notification Request，不直接拥有 outbox SQL、delivery、provider 或 retry 传输规则。
- [x] Outbox Adapter 负责原子持久化投影和向独立 Notification Engine 的 durable handoff。
- [x] accepted、retryable、dead-letter、idempotency 和 restart recovery 规则集中在 Notification Request owner。
- [x] 保持独立 Notification Engine 和 `notification.db` owner，不扩展或恢复冻结的 Gateway Notification Center 路径。
- [x] 新 owner 可用的同一变更中删除旧调用方 handoff 实现，不保留 wrapper 或双投影。
- [x] Notification Request/outbox contracts、restart recovery 和直接 Incident/Investigation/Change consumers 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Gateway Notification Request handoff 归属 `notification_requests.py`，typed request builder 只构造 contract payload，最小 Outbox Adapter Interface 为 `persist_notification_request_in(conn, request, now=...)`、`NotificationOutbox` query/metrics/handoff 与 `start_notification_handoff(..., change_reconciler=...)`。Incident、Investigation 与 Change 在既有业务 transaction 内把 typed request 交给该 Seam，SQL、幂等与 handoff 状态不泄漏到领域 Module。独立 Notification Engine 的 accepted、retryable、dead-letter、idempotency、delivery lease 与 restart recovery 归属 `NotificationRequestLifecycle`，继续只使用 `notification.db`；旧 `enqueue_*`、`NotificationStore`、change reconciliation 反向依赖、私有 database 访问与转发 wrapper 已删除。

500+ 行文件确认：Gateway `change_requests.py` 与 `incident.py` 仍分别归属 Change Request 与 Incident Module，公开 Interface 不变，本票只替换 typed request handoff；`main.py` 属于进程装配，公开入口为 `main`/`GatewayHandler`，只显式注入 shared Gateway Database 的 change reconciler；`notification_service/requests.py` 属于 Notification Request/Delivery lifecycle，公开 Interface 为 `NotificationRequestLifecycle`。上述文件完成时分别为 773、800、796、796 行，均未超过 800；被修改的 500+ 行测试文件本身即为可独立运行 selector。定向 selector 为 Gateway Notification Request、Change retry、Incident/Investigation/Change consumers、Notification lifecycle/configuration/readiness/noise、restart flow、HTTP boundary 与 observability 测试文件。

主线程复验为 96 passed；14 个 Connector readiness/heartbeat fixture 失败在 `dc791bf` 同组原样复现，另有 wrapper 删除后的 8 个直接测试通过。静态编译与 `git diff --check` 通过。固定评审基准为 `dc791bf`；Spec 最终复审无 finding，Standards 首选代理连续两次断流后按规则使用 `fallback`，补齐体量记录并把私有装配文本断言改为 required callback Interface 检查后无剩余阻塞项。

## A08 收紧 Connector Command 生命周期

**What to build:** Connector 领取、开始、回报和恢复 Command 的行为保持不变，同时 lease、terminal result、Unknown Outcome 和 reconciliation 不变量由 Connector Command lifecycle Module 管理。

**Blocked by:** A04 收敛 Connector Enrollment owner.

- [x] Connector Command lifecycle Interface 集中 claim lease、start、terminal result、lease expiry、Unknown Outcome 和 reconciliation 状态转换。
- [x] Connector Enrollment 只提供已验证 identity 与 Cluster facts，不向 Command lifecycle 泄漏 SQL 或内部表结构。
- [x] mutation Command 在 Unknown Outcome 下继续禁止自动 retry，Observed Effect 不被提升为可信执行成功。
- [x] Gateway authorization、Approval、Execution Grant、idempotency 和 audit 语义保持不变。
- [x] 新 lifecycle 可用的同一变更中删除旧分散实现，不保留双状态机或兼容分支。
- [x] Command lifecycle、Gateway/Connector contract、restart journal recovery 和 Kubernetes mutation safety selectors 通过。
- [x] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

完成记录（2026-08-09）：Connector Command lifecycle 归属 `ConnectorCommands`，公开 transaction Interface 集中 validation/reconciliation queue、mutation lease、start deadline、reject/redact、transport expiry claim 和只读 facts，`poll`、`start`、`submit_result`、lease cleanup 与 Unknown Outcome reconciliation 保持既有公开行为；terminal result 细节保留为 Module internal implementation。Validation、Execution、Cancellation、Reconciliation、Secure Input 与 Unknown Outcome projection 只消费该 Interface，旧 `connector_validation_commands.py`、`secure_input_transport.py` 及调用方 Command/Lease runtime SQL 已删除。`kubernetes_unknown_outcomes.py` 只保留 Execution/phase projection 与 audit；后台 reconciler 仍只推进 Command，既有 poll/dispatch 路径才投影 Execution。Transport expiry 与 failed observation 在 Gateway transaction 取得写锁后重新读取并应用，避免可信晚到结果被旧快照覆盖。

Enrollment 继续拥有 verification registration facts，但 Command lifecycle 不再消费其 bookkeeping；`ConnectorEnrollmentHTTPAdapter` 在同一只读 transaction 内组合 verification IDs 与 `ConnectorCommands.read_history_in` 的窄 facts，保持 cluster admin contract 与 A04 snapshot 并发保证。Gateway authorization、Approval、Execution Grant 消费、idempotency、audit、mutation no-retry、Observed Effect 用户接受规则、schema/API/OpenAPI、Connector worker/journal 和进程边界均未改变。

500+ 行文件确认：`connector_commands.py` 属于 Connector Command lifecycle Module，固定基准 559 行、完成 764 行；`kubernetes_change_executions.py` 属于 Approval/Grant/Execution projection，798→778；`kubernetes_reconciliation.py` 属于 Unknown Outcome observation/acceptance projection，757→751；`connector_enrollments.py` 仍为 Enrollment owner，799→799。测试文件 `test_gateway_connector_commands.py` 269→671，公开 selector 覆盖六个 lifecycle tracer bullets 与 transport race；`test_gateway_connector_command_races.py` 124 行；`test_gateway_kubernetes_change_executions.py` 792→791；`test_gateway_kubernetes_phase_approvals.py` 800→799；`test_gateway_kubernetes_plan_execution.py` 696→693。`main.py` 395→396 且只增加显式装配。

TDD 红绿覆盖 mutation single-use claim、start deadline/journal reconciliation、validation queue、Secure Input redaction、unstarted reject、reconciliation queue，以及 transport claim 与 failed observation 两个 SQLite/WAL 竞态。实现代理最终定向与直接消费者为 129 passed、2 skipped；14 个 phase/restart/observability fixture 失败与 `fb07466` 的 node 集合完全一致，standalone Validation 的 8 个 migration-order 失败也与基准一致。主线程最终复验为 32 passed，`compileall`、`git diff --check`、旧 import、显式 owner 装配、runtime SQL ownership 与行数门禁通过。首轮 Spec/Standards findings 已修复；固定基准 `fb07466` 的 Spec 最终复审无 finding，Standards 首选 reviewer 连续两次断流后按规则复用 `fallback`，最终复审无 finding。
