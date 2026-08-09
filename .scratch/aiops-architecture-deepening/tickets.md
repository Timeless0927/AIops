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

- [ ] Session Module 只暴露 issue、lookup、freshness、revoke 和 actor projection 所需的最小 Interface。
- [ ] Identity Administration Module 拥有 User、Team、Team Membership 和 Role Binding mutation 及其不变量，不隐式创建 Identity Adapter。
- [ ] Audit Module 拥有 denial、mutation result 和 query 记录规则，并与业务 mutation 共享 Gateway-owned transaction。
- [ ] 迁移后从总 Store 删除旧实现，不保留 wrapper、双写、双读或第二套 admin owner。
- [ ] Auth、Admin、Session invalidation、last administrator 和 audit rollback 的 Module 与 HTTP contracts 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

## A04 收敛 Connector Enrollment owner

**What to build:** Platform Administrator 与 Connector 的 enrollment、credential rotation、registration、heartbeat 和 verification 行为保持不变，同时 Connector Enrollment 由独立 Module 管理。

**Blocked by:** A03 收敛 Session、Identity Administration 与 Audit owner.

- [ ] Connector Enrollment Module 拥有 enrollment、credential、Cluster presence、registration、heartbeat 和 read verification 不变量。
- [ ] Gateway 数据库只共享连接、migration 和 transaction；其他 Module 不通过总 Store 获取 Connector Enrollment 业务状态。
- [ ] Command owner 只通过明确 Interface 使用 enrollment facts，不共享 enrollment SQL 或内部表结构。
- [ ] 新 owner 可用的同一变更中删除旧实现，不保留 compatibility facade 或双 owner。
- [ ] Connector Admin、register/heartbeat、credential rotation、verification 和 restart 直接 contracts 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

## A05 加深 Console Chat 会话 Controller

**What to build:** User 的 Chat 页面行为保持不变，同时视图通过一个会话 controller Interface 使用 SSE、query cache、取消、分支和 Attachment 生命周期。

**Blocked by:** None — can start immediately.

- [ ] Chat Session controller 拥有 query/mutation 协调、SSE 事件应用、AbortController、取消、retry/edit/reload 和 Attachment 生命周期规则。
- [ ] Chat 视图只拥有展示、输入和 transient UI state，不建立浏览器端的第二套后端状态机。
- [ ] server state、URL state 与 transient UI state 的 owner 清晰，页面不再暴露大量跨生命周期回调。
- [ ] 迁移使用现有 assistant-ui、TanStack Query 和 shadcn/ui，不增加 facade、Context、状态库或第二套 UI library。
- [ ] 旧页面逻辑在新 controller 可用的同一变更中删除，不保留双事件流或兼容 mode。
- [ ] Chat controller/page Vitest、TypeScript no-emit、production build 和现有 Chat Playwright selectors 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

## A06 建立 Clean Acceptance Ledger

**What to build:** Release Owner 看到的 Clean Acceptance 结果保持不变，同时 frontier、attempt、resume、hash、attestation 和 evaluator correction 不变量由一个 ledger Module 管理。

**Blocked by:** None — can start immediately.

- [ ] Ledger Interface 接受 typed Gate Result，并集中维护 frontier、attempt、resume、hash、attestation、eligibility 和 correction 不变量。
- [ ] Gate Runner、HTTP、Kubernetes、Notification、artifact 和 signing I/O 保留在 Adapter，不进入 ledger 领域决策。
- [ ] 本票涉及的完整 ledger 能力从超大 Evidence owner 迁出并删除旧实现；超大文件总行数不得高于票开始时。
- [ ] process interruption、safe continuation、no-I/O correction、failed gate 和 seal 行为保持现有 contract。
- [ ] Ledger Module tests 不访问真实外部服务；直接 conductor、notification gate、connector gate 和 acceptance contract selectors 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

## A07 收紧 Notification Request handoff 生命周期

**What to build:** Incident、Investigation 和 Change 的通知行为保持不变，同时领域 Module 通过 typed Notification Request Interface 完成 durable handoff，retry 与 delivery 规则由真实 owner 管理。

**Blocked by:** None — can start immediately.

- [ ] 领域 Module 只构造 typed Notification Request，不直接拥有 outbox SQL、delivery、provider 或 retry 传输规则。
- [ ] Outbox Adapter 负责原子持久化投影和向独立 Notification Engine 的 durable handoff。
- [ ] accepted、retryable、dead-letter、idempotency 和 restart recovery 规则集中在 Notification Request owner。
- [ ] 保持独立 Notification Engine 和 `notification.db` owner，不扩展或恢复冻结的 Gateway Notification Center 路径。
- [ ] 新 owner 可用的同一变更中删除旧调用方 handoff 实现，不保留 wrapper 或双投影。
- [ ] Notification Request/outbox contracts、restart recovery 和直接 Incident/Investigation/Change consumers 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。

## A08 收紧 Connector Command 生命周期

**What to build:** Connector 领取、开始、回报和恢复 Command 的行为保持不变，同时 lease、terminal result、Unknown Outcome 和 reconciliation 不变量由 Connector Command lifecycle Module 管理。

**Blocked by:** A04 收敛 Connector Enrollment owner.

- [ ] Connector Command lifecycle Interface 集中 claim lease、start、terminal result、lease expiry、Unknown Outcome 和 reconciliation 状态转换。
- [ ] Connector Enrollment 只提供已验证 identity 与 Cluster facts，不向 Command lifecycle 泄漏 SQL 或内部表结构。
- [ ] mutation Command 在 Unknown Outcome 下继续禁止自动 retry，Observed Effect 不被提升为可信执行成功。
- [ ] Gateway authorization、Approval、Execution Grant、idempotency 和 audit 语义保持不变。
- [ ] 新 lifecycle 可用的同一变更中删除旧分散实现，不保留双状态机或兼容分支。
- [ ] Command lifecycle、Gateway/Connector contract、restart journal recovery 和 Kubernetes mutation safety selectors 通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。
