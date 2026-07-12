Status: ready-for-agent

# AIOps V1 控制面与 Console 模型规格

## Problem Statement

当前 AIOps 已经拥有 Gateway、Diagnosis、Connector、MCP、审批、通知和 Console 的部分实现，但这些能力是在不同阶段逐步叠加出来的。Gateway 仍直接依赖旧的 Agent Run、Diagnosis Session、通用 panel 和多份模块级 SQLite；Connector 通信、审批、证据、通知和资源归属尚未形成一个一致的领域模型。旧 Console 将请求、状态和页面堆在一个 4087 行组件与 1313 行样式中，信息结构和视觉质量都无法继续维护。

这使得系统很难回答几个基本问题：一个告警属于哪个真实资源和 Incident，一次调查产生了哪些可信证据，谁有权批准哪个环境中的动作，动作是否真的执行过，失败通知是否可恢复，以及刷新浏览器或重启服务后哪些事实仍然成立。现有 Feishu-only 通知实现也不能满足飞书、钉钉和邮件并存的需求。

V1 需要先建立一个简单、可持久化、可审计的模型：单组织、多 Kubernetes Cluster；Gateway 持有产品状态和治理规则；每个 Cluster 由一个主动连接的 Connector 代表；Diagnosis 只负责执行诊断；Notification Engine 独立负责多渠道投递；Console 围绕 Incident 工作流完全重写。当前阶段验证这个模型和主链路，不追求生产级高可用、备份或完整自动化验收。

## Solution

AIOps V1 建立一个 Kubernetes-only、单副本的控制面。Gateway 使用一个 SQLite 数据库持久化 Incident、Investigation、Evidence Step、身份、资源关系、Approval、Connector Command、审计、报告和 Notification Request。Diagnosis、Notification Engine 和每个 Connector 分别拥有自己的 SQLite 数据库，并通过明确的 HTTP 契约交互，不跨进程共享表。

每个 Cluster 的 Connector 配置中央 Gateway 地址，主动完成注册、心跳、HTTPS 长轮询、命令开始确认和结果上报。Gateway 不需要访问受管 Cluster 的入站地址。告警被关联成 Incident；每个 Incident 拥有顺序 Investigation、持久 Investigation Event 和 Gateway-owned Evidence Step。Diagnosis Request、Diagnosis Job、Connector Command 和 Notification Delivery 都先持久化再异步推进，服务重启或暂时不可用不会把已经接受的工作静默丢失。

所有 Cluster mutation 默认关闭。诊断模型只能提出 Recommended Action，Gateway 的确定性 Evidence Gate 决定动作是否可审批。只有 Approval Authority 同时覆盖 Environment 和真实 Resource Binding 的 User，才能在 Console 中手工点击 `批准并执行`。V1 仅允许 Deployment restart、scale 和显式 revision rollback；已开始但结果不可信的 mutation 进入 Unknown Outcome，绝不自动重试。

独立 Notification Engine 接收 channel-neutral Notification Request，按第一条匹配 Notification Route fan-out 或 suppress，使用安全、可版本化 Notification Template 渲染，并通过进程内 Apprise library 投递到飞书群机器人、钉钉群机器人或 SMTP。渠道凭据加密存入 `notification.db`，数据库加密密钥由独立 Kubernetes Secret 只读挂载。

Console 在 monorepo 的 `apps/aiops_console_web` workspace 中从零重写，使用 React 19、Vite 7、React Router 7、Tailwind CSS v4、shadcn/ui 和 TanStack Query。旧组件、样式、路由和客户端状态不迁移；Console 仍独立构建、发布和部署。Console 与 `/api/v1/*`、`/auth/*` 共享一个 HTTPS origin；Gateway 拥有 OpenAPI 3.1 规格，前端从同仓库的版本化规格生成 TypeScript 类型。

## User Stories

1. As a User, I want to sign in to Console with a local AIOps identity using an HttpOnly Cookie Session and CSRF protection, so that credentials and session material are not exposed to browser scripts.
2. As an SRE, I want primary navigation to focus on Incidents, so that I can start from active operational work rather than a generic dashboard.
3. As a Platform Administrator, I want to enter a protected `/admin` surface from the user menu, so that administration remains available without competing with the SRE workflow.
4. As an SRE, I want Gateway to correlate Alert Signals by registered Cluster, namespace, Deployment Target or workload, and `alertname`, so that Pod-level noise for one workload becomes one operational case.
5. As an SRE, I want each Alertmanager fingerprint retained as an Alert Signal, so that the symptoms attached to an Incident remain independently traceable.
6. As an SRE, I want an unresolved resource to create an isolated unbound Incident, so that alert labels cannot silently create a permanent Resource Binding.
7. As an SRE, I want all Alert Signals to recover and remain stable before an Incident resolves, so that one recovered signal or a short flap cannot close active work.
8. As an SRE, I want a later firing signal to reopen the same Incident only within the configured reopen window, so that related recurrence is preserved without merging unrelated history forever.
9. As an SRE, I want one Workbench request to return Incident facts, resource context, Alert Signals, Investigation state, Evidence Steps, judgment, Recommended Actions, responsibility and actor capabilities, so that the first render is coherent.
10. As an SRE, I want the Console to hide run IDs, session IDs and persistence records, so that I work with Incident and Investigation concepts rather than service internals.
11. As an SRE, I want an Incident to have ordered Investigations and at most one active Investigation, so that repeated actions cannot create competing diagnosis threads.
12. As an SRE, I want an explicit reinvestigate action after a terminal Investigation, so that a new round is intentional and auditable.
13. As an SRE, I want every Investigation Event persisted before SSE delivery, so that refreshes and disconnects do not erase accepted progress.
14. As an SRE, I want SSE to resume from a cursor or `Last-Event-ID`, so that reconnecting Console sessions recover missed events without reconstructing a second state machine.
15. As an SRE, I want to provide, correct or retract Human Input without overwriting its history, so that operational context outside automated evidence can influence diagnosis while mistakes remain auditable.
16. As an SRE, I want Human Input to remain distinct from Approval, so that chat, notes and natural-language confirmation can never authorize a Cluster mutation.
17. As an SRE, I want each Evidence Step to show its purpose, state, result and evidence references, so that I can understand how the current judgment was formed.
18. As an SRE, I want partial, failed and skipped Evidence Steps to expose missing-evidence guidance, so that uncertainty is visible instead of being rendered as empty success.
19. As an SRE, I want Gateway's deterministic Evidence Gate to control approvability, so that AI confidence and Human Input cannot bypass missing, stale or mismatched evidence.
20. As an SRE, I want a Diagnosis Request to remain durable while Diagnosis is unavailable, so that an Incident visibly waits rather than silently losing work.
21. As an SRE, I want Diagnosis to persist a Diagnosis Job before accepting it and to retry writeback independently, so that a writeback failure never reruns completed diagnosis work.
22. As a User with Approval Authority, I want eligibility determined by both Environment and real resource scope, so that authority follows organizational responsibility rather than a broad platform role.
23. As a User with sufficient Approval Authority, I want to approve my own request when it is within my scope, so that V1 does not impose an unnecessary requester-approver split.
24. As a Platform Administrator without Approval Authority, I want mutation approval to remain unavailable, so that administration does not imply operational authority.
25. As an eligible User, I want to review a frozen Recommended Action, its Evidence Steps, target, safeguards and Rollback Plan before manually clicking `批准并执行`, so that approval is explicit and informed.
26. As an eligible User, I want Gateway to atomically record Approval, issue one Execution Grant and persist one Connector Command, so that a partial approval transaction cannot exist.
27. As an eligible User, I want stale action hashes, evidence, Resource Bindings, target state or expiry to return `action_stale`, so that changed actions require fresh review and Approval.
28. As an eligible User, I want V1 mutations limited to Deployment restart, bounded scale and rollback to an explicit existing revision, so that arbitrary commands cannot reach a Cluster.
29. As an eligible User, I want an approved conditional Rollback Plan to execute only after its predefined post-check failure, so that rollback is bounded by what I reviewed.
30. As an SRE, I want an executed mutation with no trustworthy result to become Unknown Outcome, so that the platform never retries a possibly successful mutation automatically.
31. As a Platform Administrator, I want to manage Users, Teams, Team Memberships, Role Bindings and Approval Authority in `/admin`, so that human authorization is based on registered identities.
32. As a Platform Administrator, I want sensitive administration to require authentication within the last five minutes, so that an unattended session cannot silently change critical authority or policy.
33. As a Platform Administrator, I want disabling a User or changing that User's password, roles, scope or authority to revoke all of their Sessions, so that removed authority stops immediately.
34. As a Platform Administrator, I want the final active Platform Administrator protected from removal or disablement, so that the platform cannot lock itself out through ordinary administration.
35. As a Platform Administrator, I want to review Discovery Candidates and bind real Deployment Targets to Services and Teams, so that AIOps owns a minimal Resource Catalog without depending on a CMDB.
36. As a Platform Administrator, I want confirmed Resource Bindings protected from later label hints, so that inferred metadata cannot overwrite human-confirmed ownership.
37. As a Platform Administrator, I want to create a one-to-one Connector Enrollment and configure Cluster Environment and mutation enablement, so that Cluster presence and risk policy are governed explicitly.
38. As a Platform Administrator, I want a Cluster to appear only after its first authenticated Connector Registration, so that Console configuration cannot invent runtime presence.
39. As a Connector, I want to configure Gateway's address and actively register, heartbeat, long-poll and submit results, so that Gateway never needs inbound access to my Cluster.
40. As a Connector, I want each Connector Command persisted in a local execution journal, so that a restart does not erase started execution or an unreported result.
41. As a Connector, I want to validate the Execution Grant, Cluster, namespace, action and typed parameters independently, so that a malformed Gateway request cannot become arbitrary Kubernetes access.
42. As a Connector, I want to report command start under a Command Lease and receive acknowledgement before mutation, so that Gateway can distinguish unstarted work from Unknown Outcome.
43. As a business workflow owner, I want Gateway to persist a channel-neutral Notification Request before handoff, so that Notification Engine downtime never rolls back Incident, Approval or execution state.
44. As a notification administrator, I want to configure and test Feishu group-bot, DingTalk group-robot and SMTP/TLS Notification Destinations, so that one engine supports the required delivery channels.
45. As a notification administrator, I want destination credentials encrypted in `notification.db` and masked everywhere else, so that channel configuration does not depend on Kubernetes Secret objects or expose secret material.
46. As a notification administrator, I want ordered first-match Notification Routes with a required default route, so that exact event, severity, Environment, Team and Service rules are predictable.
47. As a notification administrator, I want a matching Notification Route to fan out to destinations or explicitly suppress with a reason, so that suppression and delivery remain intentional and auditable.
48. As a notification administrator, I want to copy a built-in Notification Template and edit only safe presentation fields and whitelisted variables, so that customization cannot execute code or inject arbitrary HTML.
49. As a notification administrator, I want a template preview or test before activation and a frozen template version on each Notification Delivery, so that later edits cannot rewrite delivery history.
50. As a notification administrator, I want quiet hours, hourly limits, digest intervals and scoped Notification Silences, so that routine events do not overwhelm recipients while critical events remain visible by default.
51. As a notification administrator, I want retryable provider failures to back off and exhausted work to enter dead-letter, so that I can repair configuration and manually redeliver without losing history.
52. As a notification administrator, I want the system to state that delivery is at least once, so that a rare duplicate after an ambiguous provider response is not mistaken for an idempotency defect.
53. As an authorized report user, I want an Incident Report draft after the Incident resolves and all Investigations become terminal, so that the report is based on a complete frozen source revision.
54. As an authorized report user, I want recorded facts immutable while narrative fields remain editable, so that publication preserves evidence while allowing clear human explanation.
55. As an authorized report user, I want to publish an immutable report version manually, so that a reopened and re-resolved Incident creates new history without changing the old publication.
56. As an auditor, I want identity, policy, Approval, Execution Grant, Connector Command, rollback, Unknown Outcome, silence and redelivery decisions linked by request and correlation IDs, so that responsibility can be reconstructed without exposing secrets.
57. As a platform operator, I want bounded-label Prometheus metrics and structured JSON logs, so that unavailable services, stalled durable work, Connector heartbeat age, Unknown Outcome, dead-letter and SQLite failures are observable.
58. As a platform operator, I want each stateful process to own one database, so that transactions, schema migrations, retention and later PostgreSQL migration follow service ownership.
59. As a platform operator, I want governance records retained while acknowledged technical churn expires, so that audit history remains durable without unbounded heartbeat, Job or delivery growth.
60. As a Console user, I want a compact shadcn/ui operational interface whose server state comes from Gateway, so that the UI is readable and remains correct after refresh, reconnect or concurrent action.
61. As a Console developer, I want Gateway's OpenAPI 3.1 artifact to be the only `/api/v1` type source, so that independently released Console and Gateway artifacts do not maintain divergent DTOs.
62. As a release operator, I want `aiops-console` built independently, tagged by commit and promoted by digest, so that Console and Gateway can be upgraded or rolled back independently.

## Implementation Decisions

### 当前范围与后置项

- 当前模型阶段实现并验证 V1 领域模型与主链路，运行形态限定为 Kubernetes、单副本和每进程一个 SQLite 数据库。
- 当前验收边界包含：授权、显式 Approval 与禁止自动 mutation 的后端安全检查；前端 TypeScript 检查与生产构建；一条证明未批准不会创建 Connector Command 的负向 smoke；一条使用 fake AI、fake Connector 与 fake Notification Destination 走通批准执行、结果、Report 和 Notification 的正向 smoke；Diagnosis Request、Connector Command 与 Notification Delivery 的最小进程重启恢复检查。
- PostgreSQL、备份与恢复、HA、真实外部 Provider/AI、完整浏览器矩阵、真实 Kubernetes mutation、Kubernetes 级故障注入和生产发布验证全部后置；进入试运行或生产准备阶段时再单独收口生产验收与恢复决策，本规格不新增生产级测试 ADR。

### Scope And Deployment

- V1 serves one organization and multiple Kubernetes Clusters.
- Kubernetes is the only supported product deployment. Docker Compose may remain only for development and image smoke.
- Each control-plane process has one active replica and SQLite state on a PVC. PostgreSQL and multi-replica HA are deferred.
- Gateway, Diagnosis, Notification Engine, Console, each Connector and each MCP facade remain explicit process boundaries.
- Internal control-plane calls use dedicated ServiceAccounts, short-lived projected tokens for the `aiops-internal` audience, TokenReview authorization and NetworkPolicy-restricted ClusterIP HTTP.
- Console, Alertmanager and cross-Cluster Connector traffic use HTTPS. V1 does not add internal mTLS.

### State Ownership

- Gateway owns one `gateway.db`; Diagnosis owns one `diagnosis.db`; Notification Engine owns one `notification.db`; each Connector owns one local `connector.db` execution journal.
- Processes never access another process's tables. Modules inside one process share its transaction boundary.
- Each database uses explicit forward migrations, foreign keys, constraints and application-generated identifiers. V1 does not add a speculative dual SQLite/PostgreSQL storage abstraction.
- Gateway retains governance records indefinitely in V1. Acknowledged Connector journal data and terminal Diagnosis internal data expire after 30 days; terminal Notification Request and Notification Delivery data expire after 90 days. Unresolved dead-letter and Unknown Outcome records never expire automatically.
- V1 deliberately has no backup facility. PVC loss or corruption can lose local state; PostgreSQL adoption triggers a separate backup, RPO/RTO and restore decision.

### Connector And Cluster Boundary

- Each Cluster runs exactly one Connector configured with Gateway's address.
- Connector Enrollment preauthorizes one Connector identity bound to one immutable Cluster identity. The first authenticated Connector Registration creates visible Cluster presence.
- Connector initiates registration, heartbeat, authenticated HTTPS long polling and result submission. Gateway never initiates a connection to a managed Cluster.
- Gateway persists each Connector Command and grants a short Command Lease atomically. Connector reports start and receives acknowledgement before mutation.
- An expired unstarted command may requeue. A started read may use bounded retries. A started mutation never retries automatically and becomes Unknown Outcome when its trustworthy result is missing.
- Connector validates exact typed actions and resource scope and rejects arbitrary shell, argv and unsupported Kubernetes resource kinds.

### Identity, Resource Catalog And Authorization

- V1 uses local Users, Teams, Team Memberships and Role Bindings. LDAP and OIDC are deferred.
- Passwords use Argon2id hashes. Browser authentication uses HttpOnly Cookie Sessions and CSRF protection. A bootstrap administrator is used only for first access and recovery.
- Platform Administrator manages identity, Resource Catalog, Connector Enrollment and Cluster policy but receives no implicit Approval Authority.
- Approval Authority is an explicit binding over the intersection of Environment and real resource scope. A qualified requester may approve their own action.
- Resource Catalog owns Teams, Services, Deployment Targets and confirmed Resource Bindings. Connector discovery creates Discovery Candidates; Alert Signal labels may suggest but never establish permanent identity or ownership.
- Sensitive administrative writes require fresh authentication within five minutes and audited before/after values. Authority reduction, disablement and password changes revoke target Sessions immediately.

### Incident And Investigation Model

- Active Incident correlation uses Cluster, namespace, Deployment Target or workload identity, and `alertname`; fingerprint remains the Alert Signal identity. Cross-`alertname` correlation is deferred.
- All current Alert Signals must recover before a Recovery Observation begins a stability window. Refiring cancels it, and active mutation, rollback or post-check delays resolution.
- An Incident owns ordered Investigations and has at most one active Investigation. The first is created with alert ingress; later Investigation creation is explicit.
- Investigation lifecycle states are `queued`, `running`, `paused`, `human_led`, `completed`, `failed` and `terminated`. Execution phases are not persisted lifecycle states.
- Gateway persists ordered, idempotent Investigation Events before SSE. Workbench snapshot and cursor provide the initial handoff; event history is paginated and replayable.
- Human Input is a User-contributed assertion, context update or choice recorded as an immutable Investigation Event; it is neither Evidence nor Approval.
- Correcting or retracting Human Input appends a new Investigation Event that references the original. The original remains visible, conflicting inputs remain explicit, and no accepted history is overwritten.
- Human Input never satisfies Evidence Gate. Diagnosis may use it to request a separate Evidence Step; only that step's scoped observations can contribute to the gate.
- A relevant Human Input correction or retraction invalidates dependent judgment and makes dependent Recommended Action versions stale until they are recomputed.

### Diagnosis, Evidence And Recommended Actions

- Gateway persists Diagnosis Request and retries delivery until accepted, rejected, expired or cancelled. Diagnosis persists Diagnosis Job before returning `202`.
- Request delivery, Diagnosis Job execution and result writeback have independent retry boundaries. Evidence-source failure produces partial or human-needed outcomes instead of repeating completed work.
- Gateway owns canonical Evidence Steps. Diagnosis submits idempotent facts; MCP services and Connector return observations without owning product state.
- Evidence Step states are `running`, `succeeded`, `partial`, `failed` and `skipped`.
- Evidence Gate deterministically checks scope, freshness, reference integrity and action-specific evidence. Incomplete evidence may produce a visible judgment and guidance but never an approvable mutation.
- Recommended Action versions are immutable and hashed over target, parameters, evidence, safeguards and rollback.

### Approval And Mutation

- Every Cluster is read-only until a Platform Administrator explicitly enables mutation.
- V1 supports only `restart_deployment`, `scale_deployment` and `rollback_deployment` to an explicit existing revision.
- `批准并执行` is one idempotent Gateway command. Gateway revalidates Approval Authority, Resource Binding, policy, Evidence Gate, action version, target state, expiry and Connector availability before atomically creating Approval, Execution Grant and Connector Command.
- Approval never follows from text, notification or diagnostic model output. The Console must render a dedicated consequence-labelled control.
- Execution Grant is short-lived, exact-scope and single-use. Connector performs independent validation, preflight, execution lock, post-check and rollback handling.
- A displayed Rollback Plan may be preauthorized with the primary action, but may run only for its frozen post-check condition and matching target state.

### Incident Report

- One Incident Report aggregates all Investigations. It is not a Diagnosis or individual Investigation report.
- Gateway creates a structured draft only after Incident resolution and terminal Investigations, tied to the source revision, included Investigation IDs and evidence references.
- Users may edit narrative impact, root-cause explanation, resolution summary and follow-up fields; frozen facts remain immutable.
- Publication is manual and immutable. A reopened then re-resolved Incident produces a new draft and does not alter earlier publications.

### Notification Engine

- Notification Engine is an independent Deployment and state owner. Gateway persists a channel-neutral Notification Request and retries authenticated handoff until Notification Engine durably accepts it.
- Notification Request uses a versioned event ID, typed event, occurrence time, normalized `info`, `warning`, `error` or `critical` severity, subject, Environment/resource scope, concise summary, validated facts and relative Console path. It contains no destination, recipient, template, credential, arbitrary JSON, raw log or internal run/session ID.
- Notification Route evaluation is explicit priority and first enabled match. Exact event, severity, Environment, Team and Service matches may fan out or suppress; a default route is required.
- V1 uses the in-process BSD-2-Clause Apprise library as its only Provider Adapter for Feishu group-bot, DingTalk group-robot and authenticated SMTP/TLS. It does not deploy Apprise API or add parallel provider-specific clients.
- Destination credentials are authenticated-encryption ciphertext in `notification.db`. The encryption key is supplied only through a dedicated Kubernetes Secret mounted read-only into Notification Engine; the database/PVC, API, logs and rendered configuration never contain the plaintext key. Losing either the database or Secret requires administrators to re-enter destination credentials until production backup and recovery are defined.
- Built-in Notification Templates exist for each event/provider. Custom templates use only whitelisted `{{field}}` variables and safe presentation fields; no loops, conditions, functions, scripts or arbitrary HTML. Preview or test is required before activation.
- Quiet hours, hourly limits and digest intervals may defer lower severity. `critical` bypasses by default. A fresh-authenticated, reasoned, audited, time-bounded Notification Silence may suppress `critical`.
- Notification Delivery is at least once. Retryable network, timeout, `429` and `5xx` failures use bounded exponential backoff and `Retry-After`; non-retryable `4xx` and exhausted attempts enter dead-letter. Manual redelivery follows configuration repair.
- Apprise owns transport protocol details only. Notification Engine continues to own authorization-facing management APIs, routing, template versions, noise controls, delivery history, retry/dead-letter/redelivery and audit; Console never receives Provider credentials or calls Apprise directly.

### Console And API

- `apps/aiops_console_web` is a greenfield replacement workspace. Before implementation, its imported source is preserved in monorepo Git history; the existing source tree, CSS, route tree, API wrappers and state flows are then replaced rather than migrated.
- The stack is React 19, TypeScript, Vite 7, React Router 7, Tailwind CSS v4, shadcn/ui and TanStack Query.
- shadcn/ui is the only general-purpose component system. Its UI layer remains free of domain behavior; Incident, Evidence Step, Recommended Action, Approval, report and administration components compose the primitives in a compact work-focused layout.
- TanStack Query is the only server-state cache. URL parameters own navigable state, and local React state owns transient UI. Redux, Zustand and a client Investigation state machine are excluded.
- Console's primary product surfaces are login, Incident list, Incident Workbench, Incident Report and one permission-gated `/admin` area with domain tabs.
- Gateway exposes `/api/v1/*`, `/auth/*` and SSE, not Console assets. Existing unversioned `/api/*` remains frozen only until replacement acceptance and is then removed.
- Gateway owns an OpenAPI 3.1 contract and publishes a versioned artifact. Console generates TypeScript types only from the versioned specification in the same repository; one small handwritten client owns Cookie, CSRF, request ID and normalized error behavior. Gateway OpenAPI, generated types, and Console callers change together; incompatible changes require a new API version.
- Console and browser APIs share one HTTPS origin. Edge routing sends static paths to Console and `/api/v1/*` plus `/auth/*` to Gateway; SSE proxy buffering is disabled.
- Console builds and releases an independent OCI image. Pull requests run lockfile install, checks and production build; main publishes immutable commit identity, and release manifests promote a verified digest manually.

### Observability And Operations

- Services expose bounded-label HTTP RED and durable-work Prometheus metrics and structured JSON stdout logs.
- Request and correlation IDs propagate through Gateway, Diagnosis, Connector and Notification Engine. Logs omit credentials, session material, raw evidence and full request bodies.
- Metrics cover service availability, queue depth/age, Connector heartbeat age, Diagnosis outcomes, Command Lease/Unknown Outcome, Notification retries/dead-letter, SSE connections, SQLite errors and storage pressure.
- V1 uses a small PrometheusRule set but does not deploy OpenTelemetry, a Collector or a tracing backend.

## Testing Decisions

- Tests target externally observable safety and product behavior through the highest existing HTTP/SSE seam. They do not assert private table layout, internal helper calls, shadcn/ui implementation details or model reasoning text.
- The current model stage keeps focused mandatory checks: backend tests for authorization, explicit Approval and prevention of automatic mutation, including removal of every `policy_grant` or `auto_execute` mutation path and prevention of automatic retry after a mutation starts; frontend TypeScript checking plus production build, without adding a browser-E2E framework; a negative deterministic HTTP smoke from Alert Signal to Recommended Action proving that no Connector Command exists before Approval; and a positive smoke using fake AI, fake Connector and fake Notification Destination through Approval, Connector Command result, Incident Report and Notification Delivery.
- Diagnosis Request, Connector Command and Notification Delivery each have one focused restart-recovery check that closes and reopens their owned SQLite store and proves accepted unfinished work resumes without duplication. Full Kubernetes restart and network fault injection remain trial-stage work.
- Gateway OpenAPI contract validation is part of the backend safety check so independently generated frontend types cannot silently drift.
- Existing Gateway identity/RBAC, approval/command and split-service functional tests are prior art to adapt. Existing old Agent Run and generic Workbench panel assertions are not target contracts.
- Real external providers, real AI models, full browser matrices, Kubernetes mutation execution, Kubernetes-level fault injection and production release verification are deferred until the trial stage.

## Out Of Scope

- PostgreSQL, multi-replica HA, automated failover, SQLite backups, CSI snapshots, object-storage replication, production RPO/RTO and restore exercises.
- Supported Docker or non-Kubernetes product deployment.
- Internal TLS/mTLS, OpenTelemetry, Collector and distributed tracing.
- LDAP, OIDC, external CMDB dependency or automatic permanent binding from alert labels.
- Automatic Approval, automatic mutation, mandatory requester/approver separation, dual-administrator approval or implicit Platform Administrator Approval Authority.
- Kubernetes mutation beyond Deployment restart, bounded scale and explicit revision rollback; arbitrary shell/argv, Pod delete, apply, patch, exec, attach and other resource kinds.
- Cross-`alertname` Incident correlation.
- Feishu or DingTalk application mode, personal messages, SMS, telephone, generic webhook, acknowledgment escalation or periodic reminders.
- Notification Route regex/script conditions and Notification Template loops, conditions, functions, scripts or arbitrary HTML.
- A second notification orchestration platform, Apprise API service, or Provider-specific transport client alongside the in-process Apprise adapter.
- Message broker, archive service or configurable retention matrix.
- SSR frameworks, generated API SDK, a second frontend component library, legacy UI mode, incremental legacy component migration or permanent old `/api/*` compatibility.
- Automatic production deployment and production-grade acceptance suites during the current model stage.

## Further Notes

- ADR-0003 through ADR-0052 are the detailed decision record and take precedence if this summary becomes ambiguous; ADR-0052 supersedes ADR-0041's independent-repository decision.
- The older Console rebuild plan and PRD are superseded because they assume Gateway static serving, a smaller route set and a client-side Investigation state machine.
- Current code remains useful as behavior inventory and migration input, but old run/session/panel terminology is not part of the public target model.
- V1 explicitly accepts PVC data-loss risk while backups are deferred. PostgreSQL adoption is not itself a backup and must include a separate recovery decision before production use.
- Implementation should proceed through narrow vertical slices, keeping the frozen old API available only until the new Console path that replaces it is accepted.
