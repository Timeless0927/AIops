# PRD：AIOps 架构加深

状态：ready-for-agent

## Problem Statement

当前代码库已有清晰的运行进程边界和 V1 领域模型，但五处高变能力仍通过浅 Module 暴露过宽 Interface：Gateway 请求入口知道完整依赖图，Console Chat 页面同时拥有视图和会话生命周期，Gateway 总 Store 继续承载多个领域 owner，Clean Acceptance 的 ledger 不变量与外部 Gate 执行交织，Notification Request 与 Connector Command 的持久化 handoff 生命周期分散在调用方之间。

这些问题不会直接增加产品能力，却会让一次局部变化触碰无关调用方、迫使测试装配完整进程、增加跨领域回归，并降低后续 agent 修改代码时的 Locality 和可验证性。

## Solution

在不改变 Gateway contract、Console 用户行为、数据库 schema、领域状态机和运行进程边界的前提下，沿现有能力 owner 逐步加深五处 Module。每次迁移把完整行为移到真实 owner 后删除旧实现，不保留 wrapper、双写、兼容 barrel 或第二套状态机。

工作拆成八张可独立验证的 tracer-bullet tickets：先用 Chat 路由证明 Gateway 请求入口 Seam，再迁移其余路由；随后收敛 Session、Identity Administration、Audit 和 Connector Enrollment 的 Store owner；Console Chat、Acceptance Ledger、Notification handoff 与 Connector Command lifecycle 按独立 frontier 推进。

## User Stories

1. 作为 User，我希望现有登录、Chat、Incident、Change、Report 和 Notification 行为保持不变，从而不受内部架构迁移影响。
2. 作为 User，我希望 Chat Session 的发送、取消、重试、分支、附件和 Investigation Handoff 行为保持一致。
3. 作为 Platform Administrator，我希望身份、Connector Enrollment、Resource Catalog 和集成管理继续遵守现有鉴权、fresh authentication、CSRF 和 audit 规则。
4. 作为 Approver，我希望 Approval、Execution Grant 和 Connector Command 的授权语义不因 Module 迁移而改变。
5. 作为 Platform Operator，我希望 Gateway、Diagnosis、Connector、Notification Engine、MCP 和 Console 的进程边界保持不变。
6. 作为 Maintainer，我希望 Gateway 请求入口只负责路由分发和进程装配，从而不需要了解每个能力的内部依赖图。
7. 作为 Maintainer，我希望每个 HTTP Adapter 只暴露调用方完成路由所需的窄 Interface，从而减少 callable 参数和跨能力耦合。
8. 作为 Maintainer，我希望 Console Chat 视图通过一个会话 controller Interface 使用异步规则，从而让 SSE、缓存、取消和附件生命周期集中维护。
9. 作为 Maintainer，我希望 Session、Identity Administration、Audit 和 Connector Enrollment 各由所属 Module 管理，从而不继续扩张 Gateway 总 Store。
10. 作为 Maintainer，我希望多个 owner 在需要原子性时共享 Gateway-owned transaction，而不是共享无边界业务 Interface。
11. 作为 Release Owner，我希望 Clean Acceptance 的 frontier、attempt、resume、hash 和 attestation 不变量集中在 ledger Module，从而能在不重放外部 I/O 的情况下验证恢复行为。
12. 作为 Maintainer，我希望 Notification Request 的持久化 handoff、retry 和 delivery 接受规则由真实 owner 管理，从而避免不同领域调用方各自解释状态。
13. 作为 Maintainer，我希望 Connector Command 的 lease、start、terminal 和 Unknown Outcome 规则集中在一个生命周期 Interface，从而禁止不安全的自动 mutation retry。
14. 作为 Maintainer，我希望每张迁移票都有独立 selector 和固定比较基准，从而可以单独审查、回滚和提交。
15. 作为 Maintainer，我希望迁移完成后旧实现被删除，从而不留下双 owner、隐式兼容路径或未来扩展占位。
16. 作为 Reviewer，我希望 Standards 与 Spec 两个评审轴分别通过，从而既能确认仓库规范，也能确认行为保持和范围完整。

## Implementation Decisions

- 五项改造全部是行为保持的责任迁移。发现的功能缺陷、产品需求或 contract 变化单独记录，不夹入本规格。
- Gateway 请求入口先选择高变 Chat 路由证明 Seam。Gateway Handler 最终只调用窄的路由分发 Interface；能力依赖由 Gateway 装配 Module 持有。
- HTTP Adapter 负责输入验证、鉴权调用、调用所属 Module Interface 和序列化结果。领域决策、持久化规则和外部客户端创建不进入 Handler。
- Console Chat 会话 controller 拥有 Chat Session 生命周期、SSE 事件协议、query cache 协调、取消和 Attachment 生命周期；视图只拥有展示和 transient UI state。
- Gateway 数据库继续共享连接、migration 和 transaction 约束。Session、Identity Administration、Audit 和 Connector Enrollment 分别拥有窄 Interface，不共享总业务 Store。
- Clean Acceptance ledger 集中 frontier、attempt、resume、hash、attestation 和 evaluator correction 不变量；Gate Runner 与外部 HTTP、Kubernetes、Notification 和 artifact I/O 继续作为 Adapter。
- Notification handoff 通过 typed Notification Request Interface 进入 outbox Adapter；领域 Module 不直接拥有 delivery、provider 或 retry 传输规则。
- Connector Command lifecycle 集中 lease、start、terminal result、Unknown Outcome 和 reconciliation 不变量；Connector Enrollment 不再向 Command owner 泄漏共享 SQL。
- 迁移采用搬家而不是复制。新 owner 可用的同一变更中删除旧实现，不保留 wrapper、双写、双读、兼容开关、第二套 route tree 或浏览器状态机。
- 保持现有 Gateway/OpenAPI、Console、SSE、数据库 schema、Kubernetes mutation governance 和 T24 retirement contract，不恢复 legacy Console 或未版本化 contract。
- 不增加依赖、factory registry、插件层或只有一个实现的代码 interface。只有真实 I/O Adapter 和已存在替换需求保留 Seam。
- 每张 ticket 独立提交。完成定向测试和直接 contract 测试后，使用固定比较基准执行 Standards 与 Spec 双轴 review。
- 当前混入 Console 提交的 Gateway 草稿不构成验收。首张 ticket 必须先把最终 Gateway 改动整理为不包含无关 Console 内容的独立、可评审 diff，且不得重写或回退无关 Console 历史。

## Testing Decisions

- 测试通过公开 Interface 验证可观察行为，不断言私有 helper 调用顺序，也不为迁移复制已有案例。
- Gateway 使用现有 `/api/v1` HTTP contract 作为最高行为 Seam；必要的 architecture fitness test 只固定入口与能力 Module 的依赖方向。
- Console Chat 使用会话 controller Interface 和现有页面行为测试，直接消费者继续运行 TypeScript no-emit、production build 和现有 Chat Playwright selector。
- Session、Identity Administration、Audit 和 Connector Enrollment 通过各 owner Module Interface 测试，并运行直接 Gateway HTTP contract。
- Acceptance Ledger 通过纯 ledger Interface 验证 frontier、attempt、resume、hash 和 correction，不访问真实网络、Kubernetes 或 Notification provider。
- Notification handoff 使用 Notification Request/outbox contract 与 restart recovery selector；不发送真实通知。
- Connector Command lifecycle 使用 lease/start/result/Unknown Outcome contract 与 restart recovery selector；不执行真实 Kubernetes mutation。
- 每张 ticket 的验证顺序是受影响 Module selector、直接 contract 消费者、受影响 workspace 静态检查。全量测试留给 CI 或明确发布验收。

## Out of Scope

- 新增或修改产品功能、UI、文案、API、OpenAPI schema、SSE contract 或数据库 schema。
- 改变认证、授权、Approval、Execution Grant、mutation guard、audit、幂等或 Unknown Outcome 语义。
- 改变 Gateway、Diagnosis、Connector、Notification Engine、MCP 或 Console 的进程边界。
- 恢复、迁移或包装 Legacy Console、未版本化 `/api/*`、Agent Run、Diagnosis Session 或 panel contract。
- 全量重写 Gateway、`GatewayV1Store`、Console Chat 或 Clean Acceptance。
- 引入新依赖、第二套 UI library、客户端后端状态机、通用 repository framework 或未来扩展点。
- PostgreSQL、HA、backup、真实 Provider、真实 Kubernetes mutation 或生产发布验收。

## Further Notes

- 来源是 2026-08-08 的 AIOps 架构改进审查，覆盖 Gateway 请求入口、Console ChatPage、Gateway 总 Store、Clean Acceptance ledger 和持久化 handoff 生命周期五个候选。
- 执行 frontier：Gateway 首票优先；Console Chat、Acceptance Ledger 和 Notification handoff 可以独立推进；Gateway Store 在入口迁移后串行推进；Connector Command lifecycle 等待 Connector Enrollment owner 稳定。
- `1bb2d44` 同时包含 Console 正式变更和未完成的 Gateway 草稿。该提交不作为本规格任何 ticket 的通过证据。
- 当前 Gateway auth contract 仍固定期待 migration 只到 53，而当前代码已注册 54、55。该既有测试债务不属于本规格，执行票据时须明确记录，不能误归因于架构迁移。
- 本规格不改变领域术语，也没有满足 ADR 门槛的难逆转决策，因此不修改领域词汇表或新增 ADR。
