# Tickets: AIOps V1 控制面与 Console 模型

按 [AIOps V1 控制面与 Console 模型规格](./PRD.md) 建立可持久化、可审计的 Incident 工作流，并用独立发布的 Console artifact、Connector、Diagnosis 与 Notification Engine 完成模型阶段主链路。

Work the **frontier**: any ticket whose blockers are all done. T01 是 expand 起点，T24 是 contract 终点；其余票据以可独立演示或验证的纵向切片迁移到 V1 模型。

## T01 保存 Console 基线并建立 greenfield shell

**What to build:** 在替换旧 Console 前保存可恢复的 Git baseline，然后交付一个不复用旧组件、样式、路由、API wrapper 或状态流的 V1 Console shell，为后续 Incident、Report 与管理切片提供稳定入口。

**Blocked by:** None — can start immediately.

- [x] 当前 Console 状态在 monorepo Git 历史中形成可识别 baseline，Git 历史是唯一旧源码回退记录。
- [x] 旧 source tree 被整体替换，不保留 legacy UI mode、兼容组件、平行路由树或第二份归档源码。
- [x] 新 shell 使用 React 19、TypeScript、Vite 7、React Router 7、Tailwind CSS v4、shadcn/ui 与 TanStack Query。
- [x] shadcn/ui 是唯一通用组件系统，基础 UI 组件不包含 AIOps 领域行为。
- [x] TanStack Query 是唯一 server-state cache，URL parameter 拥有可导航状态，local React state 只拥有 transient interaction；不引入 Redux、Zustand 或客户端 Investigation state machine。
- [x] 主导航只围绕 Incidents；`/admin` 从用户菜单进入，Report 保持 Incident 子页面。
- [x] TypeScript no-emit check 与 production build 通过。

## T02 落实 Kubernetes Service Identity 与网络边界

**What to build:** 让控制面进程使用 Kubernetes 原生身份进行受限的内部 HTTP 调用，使后续 Diagnosis、MCP 与 Notification Engine 切片共享同一条认证和授权边界。

**Blocked by:** None — can start immediately.

- [x] 每个内部进程使用独立 ServiceAccount 和 `aiops-internal` audience 的短期 projected token。
- [x] 接收端通过 TokenReview 认证并按 ServiceAccount identity 授权，不接受共享静态内部 secret。
- [x] NetworkPolicy 只允许预期进程访问对应 ClusterIP HTTP route，内部 route 不暴露到公共 Ingress 或 NodePort。
- [x] Console、Alertmanager 与跨 Cluster Connector 继续使用各自的外部 HTTPS 与身份机制。
- [x] 模型阶段不引入内部 TLS/mTLS、OpenTelemetry 或第二套服务认证机制。
- [x] 身份错误和越权调用通过公开 HTTP 边界 fail closed，并留下不含 token 的审计信息。

## T03 建立本地登录与 V1 契约闭环

**What to build:** User 可以从新 Console 使用本地 AIOps 身份登录并进入空的 Incident 工作区，同时建立 `gateway.db`、OpenAPI 3.1 与前端生成类型的 expand 边界。

**Blocked by:** T01 保存 Console 基线并建立 greenfield shell.

- [x] Gateway 使用一个带 forward migration、foreign key、constraint 与应用生成 ID 的 `gateway.db` 承载新的 V1 状态。
- [x] 首次访问和恢复使用 bootstrap administrator；正常密码只以 Argon2id hash 保存。
- [x] 浏览器登录使用 HttpOnly Cookie Session 与 CSRF protection，Session 状态持久化在 Gateway-owned `gateway.db`。
- [x] `/auth/*` 保持同源语义，首个 `/api/v1/*` actor/Incident contract 由 Gateway OpenAPI 3.1 定义并验证。
- [x] Console 从同仓库的版本化 Gateway OpenAPI specification 只生成 TypeScript types；手写 client 统一处理 Cookie、CSRF、request ID 与 normalized error。
- [x] 登录成功进入可用的空 Incident 列表，未登录或无权限访问均有明确且不泄露资源存在性的结果。
- [x] 旧未版本化 API 在本票中保持冻结，不增加双写或永久兼容层。

## T04 安全管理 User、Team 与 Role Binding

**What to build:** Platform Administrator 可以在受限 `/admin` 中管理本地 User、Team、Team Membership 与 Role Binding，同时权限收回会立即影响当前 Session。

**Blocked by:** T03 建立本地登录与 V1 契约闭环.

- [x] `/admin` 仅对当前授权的 Platform Administrator 可见且可访问，普通 SRE 主导航仍然只有 Incident 工作流。
- [x] 管理员可以创建、查看和更新 User、Team、Team Membership 与 Role Binding，不依赖 LDAP、OIDC 或 free-text scope。
- [x] 统一 fresh-auth gate 要求影响身份、密码、角色、Approval Authority、Connector Enrollment、Resource Binding、Cluster Environment 或 mutation enablement 的写操作必须在五分钟内重新认证。
- [x] User disable、password change、Role Binding 或 Approval Authority reduction/removal 会立即撤销目标 User 的所有 Session，授权读取当前状态而非登录快照。
- [x] 最后一个 active Platform Administrator 不能被禁用或移除。
- [x] Platform Administrator 不会因此获得 Approval Authority。
- [x] 每次管理尝试记录 actor、target、reason、before/after、result 与 request ID，且不记录 secret。

## T05 Enrollment Connector 并建立 Cluster 真实存在性

**What to build:** Platform Administrator 可以预先 Enrollment 一个 Connector；只有该 Connector 首次主动认证注册后，对应 Cluster 才在 Console 中成为真实存在的运行对象。

**Blocked by:** T04 安全管理 User、Team 与 Role Binding.

- [x] Connector Enrollment 一对一绑定 immutable `connector_id` 与 `cluster_id`，Gateway 只保存 credential hash。
- [x] 共享 credential、trust-on-first-use、identity mismatch 与重复 Cluster binding 被拒绝。
- [x] Connector 主动向 Gateway 注册并发送 heartbeat；Gateway 不需要访问受管 Cluster 的入站地址。
- [x] Enrollment 本身不创建可见 Cluster，第一次成功认证注册才创建 Cluster presence。
- [x] Console 没有手工“添加 Cluster”入口，runtime online/offline/degraded 状态只来自 Connector heartbeat。
- [x] Platform Administrator 只能编辑已注册 Cluster 的 display name、Environment、governance notes 与 policy switches。
- [x] Enrollment、credential rotation/revocation、Environment 与 mutation policy write 复用 T04 的 fresh-auth gate。
- [x] 每个 Cluster mutation 默认关闭；credential 可以独立 revoke 和 rotate。

## T06 把 Discovery Candidate 绑定到 Service 与 Team

**What to build:** Platform Administrator 可以把 Connector 发现的真实 workload 提升为 Deployment Target，并确认其 Service、Team 与 Resource Binding，使授权基于实际资源归属。

**Blocked by:** T04 安全管理 User、Team 与 Role Binding; T05 Enrollment Connector 并建立 Cluster 真实存在性.

- [x] Connector discovery 创建包含真实 Cluster、namespace 与 workload/service identity 的 Discovery Candidate。
- [x] 管理员可以创建或选择 Team、Service 与 Deployment Target，并确认 Resource Binding。
- [x] Alert Signal label 只能提供候选提示，不能创建永久 identity、ownership 或 binding。
- [x] 已确认 Resource Binding 不会被后续 label hint 或 discovery refresh 静默覆盖。
- [x] Resource Binding confirmation/correction 复用 T04 的 fresh-auth gate，并记录结构化 before/after audit。
- [x] 一个 Service 可以拥有多个 Deployment Target，Team ownership 可用于后续授权与通知范围。
- [x] 未确认资源保持明确的 unbound 状态，不能获得 Execution Grant。
- [x] V1 不要求 CMDB，也不引入 speculative CMDB adapter。

门禁记录（T06）：Resource Catalog Module 为 `apps/aiops_k8s_gateway/resource_catalog.py`，公开 Interface 是 discovery refresh、Service 创建、Binding 确认/纠正和 execution target guard，HTTP 与 grant 前置映射由 `resource_catalog_http.py` 管理，定向 selector 为 `tests/test_gateway_resource_catalog.py` 与 `tests/test_gateway_v1_resource_catalog_contract.py`。Connector identity 已迁入 `connector_identity.py`，Connector discovery Module 为 `apps/cluster_connector/discovery.py`，Gateway client Interface 为 `apps/cluster_connector/gateway_client.py`，定向 selector 为 `tests/test_gateway_v1_connectors_contract.py`、`tests/test_connector_discovery.py` 与 `tests/test_connector_registration_recovery.py`。本票触碰的超大旧文件 `main.py` 从 5517 行下降，`v1_store.py` 从 1047 行下降。`toolsets/topology_store.py` 所属 Kubernetes Inventory Module 的公开 Interface 为 `KubernetesInventory`，定向 selector 为 `tests/test_topology_store.py`。

## T07 将 Alert Signal 关联成可见 Incident

**What to build:** SRE 可以在 Console 看到 Alertmanager 输入形成的 Incident；多个已绑定或未绑定 Alert Signal 都以产品领域模型出现在 Incident 列表和 Workbench 中。

**Blocked by:** T06 把 Discovery Candidate 绑定到 Service 与 Team.

- [x] Gateway 只接受属于已注册 Cluster 的有效 Alert Signal，并保留每个 Alertmanager fingerprint。
- [x] active Incident 按 Cluster、namespace、Deployment Target 或 workload identity 及 `alertname` 关联。
- [x] 无法解析资源时按 fingerprint 创建隔离的 unbound Incident，不从 label 建立 Resource Binding。
- [x] Alert ingress 自动创建第一个 `queued` Investigation，同一 Incident 不创建并发 active Investigation。
- [x] Incident 列表清楚区分 active、resolved、bound 与 unbound 状态，并遵守 actor scope。
- [x] Workbench 首次请求返回 bounded Incident facts、resource context、Alert Signals、Investigation summary、responsibility、actor capabilities、snapshot revision 与 event cursor。
- [x] `/api/v1` response 和 error 均符合 Gateway OpenAPI，Console 使用生成类型渲染而不暴露 run/session/persistence identity。

门禁记录（T07）：Incident Module 为 `apps/aiops_k8s_gateway/incident.py`，公开 Interface 是 Alert Signal ingress、actor-scoped Incident list 与 Workbench snapshot；HTTP Adapter 为 `incident_http.py`，Alertmanager trust boundary 为 `alertmanager_webhook.py`。定向 selector 为 `tests/test_gateway_incidents.py`、`tests/test_gateway_v1_incident_contract.py`、`tests/test_gateway_alertmanager_webhook.py`，直接 contract selector 为 `tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_v1_connectors_contract.py`、`tests/test_gateway_v1_resource_catalog_contract.py` 与 `tests/test_architecture_boundaries.py`；Console 通过 Vitest、TypeScript no-emit 与 production build。`main.py` 从 5513 行降至 5508 行，`v1_store.py` 从 978 行降至 971 行，新生产文件均低于 800 行。额外全量 pytest 为 576 passed、8 failed；代表性失败已在未修改的 T06 HEAD `e6a9760` 复现，均为 legacy 测试未携带 T05 已要求的 Connector Enrollment credential，不属于 T07 回归。

## T08 按稳定窗口 resolve 与 reopen Incident

**What to build:** SRE 可以看到 Incident 只有在全部 Alert Signal 持续恢复后才自动解决，并在配置窗口内把相关复发恢复到同一个 Incident 历史中。

**Blocked by:** T07 将 Alert Signal 关联成可见 Incident.

- [x] 每个 Alert Signal 独立记录 firing 和 recovered；任一 Signal 仍 firing 时 Incident 不会 resolve。
- [x] 全部当前 Signal recovered 后记录 Recovery Observation 并开始可配置 stabilization window。
- [x] stabilization window 内任何 refire 会取消当前 Recovery Observation。
- [x] window 完成后 Incident 自动 resolve，Console 显示 recovery、stabilizing、resolved 与 reopened 状态。
- [x] Recovery Observation 作为新 evidence，使尚未执行的 Recommended Action 与 pending Approval stale；active Investigation 不阻止 resolution，且仍可完成并保留 findings。
- [x] resolved Incident 只在可配置 reopen window 内复用，超出窗口的 firing Signal 创建新 Incident。
- [x] cross-`alertname` correlation 不在本票实现。

门禁记录（T08）：Incident Module 继续由 `apps/aiops_k8s_gateway/incident.py` 拥有，公开 Interface 是 Alert Signal ingress、到期 Recovery Observation reconciliation、actor-scoped Incident list 与 Workbench snapshot；Gateway runtime Adapter 为 `incident_runtime.py`，在没有后续 HTTP 请求时也推进到期 resolution。Recovery Observation 的创建与取消均递增 `evidence_revision`，供 T11/T13 创建的 Recommended Action 与 Approval 冻结并判 stale，不接入冻结的 legacy action/approval 路径；active mutation、rollback 与 post-check blocker 在 T13 建立真实 V1 执行状态时接入同一 reconciliation。HTTP Adapter 仍为 `incident_http.py`，Alertmanager trust boundary 仍为 `alertmanager_webhook.py`。定向 selector 为 `tests/test_gateway_incidents.py`、`tests/test_gateway_v1_incident_contract.py` 与 `tests/test_gateway_alertmanager_webhook.py`，直接 contract selector 为 `tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_v1_connectors_contract.py`、`tests/test_gateway_v1_resource_catalog_contract.py`、`tests/test_architecture_boundaries.py`；Console selector 为 Vitest、TypeScript no-emit 与 production build。`incident.py` 为单一内聚 Incident 生命周期职责且低于 800 行；`main.py` 与 `v1_store.py` 未增长，未向过渡 `GatewayV1Store` 增加能力。定向与直接 contract 为 23 passed，Console Vitest 2 passed 且 production build 通过；最终全量 pytest 为 579 passed、8 failed，这 8 项已在未修改的 T07 HEAD `b1adf42` 用相同 selector 全部复现，均为 legacy 测试未携带 T05 已要求的 Connector Enrollment credential，不属于 T08 回归。

## T09 持久交付 Investigation 给 Diagnosis

**What to build:** Alert 创建的 Investigation 可以可靠交给 Diagnosis；Diagnosis 暂时不可用或 writeback 失败时，SRE 仍能看到 durable waiting/progress 状态，而不会丢失或重复诊断。

**Blocked by:** T02 落实 Kubernetes Service Identity 与网络边界; T07 将 Alert Signal 关联成可见 Incident.

- [x] Gateway 先持久化 Diagnosis Request，再以 backoff 重试交付直到 accepted、rejected、expired 或 cancelled。
- [x] Diagnosis 在返回 `202 Accepted` 前把 idempotent Diagnosis Job 写入自己的 `diagnosis.db`。
- [x] Request delivery、Diagnosis Job execution 与 result writeback 使用独立 retry boundary；writeback failure 不重复已完成诊断。
- [x] Investigation 只使用 `queued`、`running`、`paused`、`human_led`、`completed`、`failed` 与 `terminated` lifecycle state。
- [x] 同一 Incident 最多一个 active Investigation；terminal 后只有显式 reinvestigate 才创建下一轮。
- [x] evidence source failure 产生 partial 或 needs-human 结果，不因单源失败重跑整个 Job。
- [x] Workbench 显示 waiting、running 与 terminal 状态，不暴露 Diagnosis Job 或 session identity。

门禁记录（T09）：Gateway Investigation Delivery Module 为 `apps/aiops_k8s_gateway/diagnosis_delivery.py`，公开 Interface 是同事务持久化/取消 Diagnosis Request、到期/退避交付 reconciliation 与幂等 result writeback；HTTP Adapter 和 runtime Adapter 分别为 `diagnosis_delivery_http.py`、`diagnosis_delivery_runtime.py`。Incident Module 拥有 Investigation 创建与序列不变量，并公开增加 terminal 后显式 `reinvestigate` 命令；Delivery Module 只在同一 Gateway transaction 内拥有交付 accepted、rejected、expired、cancelled 和 result 驱动的 lifecycle transition，重复 Alert Signal 不会创建新轮次。Diagnosis Job Module 为 `diagnosis_service/jobs.py`，公开 Interface 是 durable accept、execution claim/finish、独立 writeback claim/ack 与持久 artifact export；进程入口 `service_main.py` 只装配 worker 和 HTTP route。Gateway 与 Diagnosis 分别使用 `aiops-gateway-data`、`aiops-diagnosis-data` PVC。定向 selector 为 `tests/test_gateway_diagnosis_delivery.py`、`tests/test_diagnosis_jobs.py`、`tests/test_gateway_incidents.py` 与 `tests/test_diagnosis_service.py`；直接 contract selector 为 `tests/test_gateway_v1_incident_contract.py`、`tests/test_gateway_alertmanager_webhook.py`、`tests/test_internal_service_auth.py`、`tests/test_diagnosis_entry.py`、`tests/test_architecture_boundaries.py` 与 `tests/test_k8s_manifests.py`。任务开始时超大 `main.py` 为 5508 行，完成时低于该值；`service_main.py` 从 680 行下降，`tests/test_diagnosis_service.py` 从 550 行下降；新文件均低于 800 行。T09 定向测试为 26 passed，最终全量 pytest 为 593 passed。

## T10 持久回放 Investigation Event 并接收 Human Input

**What to build:** SRE 可以实时跟随 Investigation、提交 Human Input，并在刷新或断线后从持久 cursor 恢复所有已接受进展。

**Blocked by:** T09 持久交付 Investigation 给 Diagnosis.

- [x] Human Input assertion、correction、retraction、诊断模型输出、tool activity、Evidence Step change 与 lifecycle transition 在确认或 streaming 前写成 idempotent Investigation Event。
- [x] Event ID 在一个 Investigation 内单调递增，历史可以分页查询。
- [x] Workbench snapshot 的 cursor 与 SSE `Last-Event-ID` 形成无丢失、无重复可见进展的稳定 handoff。
- [x] Console 使用 TanStack Query cache 追加 immutable event 并按需 invalidate snapshot，不建立客户端 Investigation state machine。
- [x] 已提交 Human Input 不可覆盖；correction 与 retraction 追加新 Event 并引用被修正记录，原文、冲突输入与完整关系链始终可见。
- [x] Human Input 不是 Evidence，永远不计入 Evidence Gate，也不创建 Approval、Execution Grant 或 Connector Command。
- [x] Diagnosis 如需核实 Human Input，必须创建独立 Evidence Step；只有该步骤取得的 scoped observation 可以参与 Evidence Gate。
- [x] 影响当前判断的 correction 或 retraction 会使依赖它的 judgment 失效，并将依赖的 Recommended Action 标记为 stale。
- [x] pause、terminate、human-led takeover 与 explicit reinvestigate 具有可见、可审计且权限受控的结果。
- [x] SSE reconnect、permission denial 和 terminal Investigation 都有明确 UI 状态。

门禁记录（T10）：Gateway Investigation Event Module 为 `apps/aiops_k8s_gateway/investigation_events.py`，公开 Interface 是事件分页回放、Human Input 追加/引用、lifecycle control 与原子事件 helper；HTTP/SSE Adapter 为 `investigation_event_http.py`。Incident Module 继续拥有 Investigation 创建和 explicit reinvestigate，Diagnosis Delivery Module 在原状态事务内写入 diagnosis、tool、Evidence Step 与 lifecycle event。Diagnosis Handoff Module 为 `diagnosis_service/handoff.py`，公开 Interface 是把 Gateway Diagnosis Request 转成明确标注 Human Input 非 Evidence 属性的 diagnosis context，定向 selector 为 `tests/test_diagnosis_service.py`；该能力先以 `cecee4b` 行为不变迁移，`service_main.py` 从任务开始的 511 行降至 485 行。Console 只通过 TanStack Query event cache 和 Workbench snapshot 消费服务端状态。定向 selector 为 `tests/test_gateway_investigation_events.py`、`tests/test_gateway_diagnosis_delivery.py`、`tests/test_gateway_incidents.py`、`tests/test_gateway_v1_incident_contract.py` 与 Console Vitest；直接 contract selector 为 `tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_alertmanager_webhook.py`、`tests/test_internal_service_auth.py`、`tests/test_diagnosis_service.py`、`tests/test_diagnosis_jobs.py`、`tests/test_diagnosis_entry.py` 与 `tests/test_architecture_boundaries.py`。本票触碰的 `incident.py` 为单一 Incident 生命周期职责，公开 Interface 是 ingress、list、Workbench 与 reinvestigate，定向 selector 为 `tests/test_gateway_incidents.py`；超大 `main.py` 从 5493 行降至 5492 行，新文件均低于 800 行。定向与直接 contract 为 54 passed，Console Vitest 4 passed 且 production build 通过；最终全量 pytest 为 590 passed、9 failed，这 9 项已在未修改的 T09 HEAD `ff459de` 用相同 selector 全部复现，均为 legacy 测试未携带 T05 已要求的 Connector Enrollment credential，不属于 T10 回归。

## T11 生成 Evidence Step 与受 Gate 约束的 Recommended Action

**What to build:** SRE 可以从 Workbench 阅读 Diagnosis 形成的 Evidence Step、当前判断与 Recommended Action，并明确知道缺失证据为何阻止 mutation approvability。

**Blocked by:** T06 把 Discovery Candidate 绑定到 Service 与 Team; T10 持久回放 Investigation Event 并接收 Human Input.

- [x] Gateway canonicalize Diagnosis 提交的 idempotent Evidence Step facts，MCP 与 Connector observation 不拥有产品状态。
- [x] Evidence Step 只使用 `running`、`succeeded`、`partial`、`failed` 与 `skipped`，每种状态都有可读结果或缺失指导。
- [x] Console 默认展示 purpose、source、scope、result、impact 与 evidence references，不默认输出 raw JSON、完整日志或内部 trace。
- [x] Evidence Gate 确定性验证 resource scope、freshness、reference integrity 与 action-specific requirements。
- [x] incomplete gate 可以产生 judgment 和 next-evidence guidance，但不能产生 approvable mutation。
- [x] Recommended Action version immutable，hash 覆盖 target、typed parameters、evidence、safeguards 与 Rollback Plan。
- [x] 确定性 HTTP smoke 使用 fake AI 与 fake Notification Destination 走通 Alert Signal → Incident → Investigation → Recommended Action，并证明没有 Connector Command。

门禁记录（T11）：Gateway Evidence/Decision Module 为 `apps/aiops_k8s_gateway/evidence_decisions.py`，公开 Interface 是在 Diagnosis writeback transaction 中 canonicalize Evidence Step、judgment 与 immutable Recommended Action，确定性计算 Evidence Gate，以及向 Workbench 投影和按 Human Input/Recovery Observation stale 决策；Diagnosis Delivery、Incident 与 Investigation Event Module 只调用该 Interface，不拥有第二份产品状态。定向 selector 为 `tests/test_gateway_diagnosis_delivery.py` 与 `tests/test_gateway_v1_incident_contract.py`，直接 contract selector 为 `tests/test_gateway_incidents.py`、`tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_v1_connectors_contract.py`、`tests/test_gateway_v1_resource_catalog_contract.py`、`tests/test_gateway_alertmanager_webhook.py`、`tests/test_internal_service_auth.py`、`tests/test_diagnosis_jobs.py`、`tests/test_diagnosis_service.py` 与 `tests/test_architecture_boundaries.py`；Console selector 为 Vitest、TypeScript no-emit 与 production build。任务开始时 `incident.py` 为 697 行，仍只有 Incident 生命周期与 Workbench 组装职责；`tests/test_gateway_diagnosis_delivery.py` 的公开 seam 是 Diagnosis result writeback 到 Workbench/Event replay，定向 selector 即该文件；新生产文件均低于 800 行。确定性 HTTP smoke 通过 fake AI HTTP writeback 和 fake Notification Destination 覆盖主链，并断言 mutation command builder 零调用。定向与直接 contract 为 54 passed，Console Vitest 5 passed 且 production build 通过；最终全量 pytest 为 592 passed、10 failed，其中 9 项已在未修改的任务固定点 `611f8a2` 用相同 selector 复现，均为 legacy 测试未携带 T05 已要求的 Connector Enrollment credential，剩余 1 项在固定点和当前 HEAD 隔离运行均通过，属于全量顺序污染而非 T11 回归。

## T12 通过长轮询执行持久只读 Connector Command

**What to build:** Gateway 可以向指定 Cluster 排队一条 durable read command，由 Connector 主动长轮询领取、确认开始、执行并回报，即使 Connector 重启也不丢已开始或未上报结果。

**Blocked by:** T05 Enrollment Connector 并建立 Cluster 真实存在性.

- [x] Gateway 在 delivery 前持久化 Connector Command，并原子授予短期 Command Lease。
- [x] Connector 只领取与自身 identity 和 Cluster 匹配的 command，并在执行前报告 start 且收到 Gateway acknowledgement。
- [x] Connector 使用本地 `connector.db` journal 保存 accepted、started 与 unreported terminal result。
- [x] expired 但未 started 的 command 可以 requeue；started read 只有 bounded retry。
- [x] identical result submission 幂等成功，conflicting result 被拒绝并审计，late result 保留用于 reconciliation。
- [x] Connector 独立验证 Cluster、namespace、read action 与 typed parameter，拒绝任意 shell/argv mutation。
- [x] Console 的 Cluster 视图可以观察 heartbeat、pending/read command 与最后结果，而不伪造 runtime state。

门禁记录（T12）：Gateway Connector Command Module 为 `apps/aiops_k8s_gateway/connector_commands.py`，公开 Interface 是 durable read command queue、原子 Command Lease、start acknowledgement、terminal result reconciliation 与 Cluster command summary；HTTP Adapter 为 `connector_command_http.py`。Connector Command Module 为 `apps/cluster_connector/command_worker.py`，公开 Interface 是 `connector.db` journal、typed `get_resource` 转换和主动长轮询 cycle，进程入口只装配 worker；部署为 Connector 独立 PVC。定向 selector 为 `tests/test_gateway_connector_commands.py`、`tests/test_connector_command_worker.py` 与 `tests/test_gateway_v1_connectors_contract.py`，直接 contract selector 为 `tests/test_gateway_v1_auth_contract.py`、`tests/test_connector_registration_recovery.py`、`tests/test_k8s_guard.py`、`tests/test_k8s_manifests.py`、`tests/test_data_dir_env.py`、`tests/test_split_service_packaging.py` 与 `tests/test_architecture_boundaries.py`；Console selector 为 Vitest、TypeScript no-emit 与 production build。任务开始和结束时 `main.py` 均为 5492 行，新生产文件均低于 800 行。定向与直接 contract 均通过，Console Vitest 5 passed 且 production build 通过；最终全量 pytest 为 599 passed、10 failed，其余 10 项与 T11 固定点 `611f8a2` 的记录一致：9 项 frozen legacy tests 未携带 T05 Enrollment credential，1 项为全量顺序污染。

## T13 显式批准并执行 Deployment restart

**What to build:** 有明确 Approval Authority 的 User 可以在 Workbench 审阅一个冻结的 restart Recommended Action，并通过唯一的 `批准并执行` 控件提交一次受治理的 Connector Command。

**Blocked by:** T11 生成 Evidence Step 与受 Gate 约束的 Recommended Action; T12 通过长轮询执行持久只读 Connector Command.

- [x] `/admin` 可以通过 T04 的 fresh-auth gate 创建同时覆盖 Environment 与真实 resource scope 的 Approval Authority；Platform Administrator 不获得隐式 authority。
- [x] 合资格 requester 可以批准自己的 action，不合资格 User 看不到可用控制且无法绕过 API authorization。
- [x] approval surface 显示 frozen target、Evidence Steps、typed parameters、safeguards、expiry 与 Rollback Plan。
- [x] Gateway 在一个 transaction 中重新验证 authority、Resource Binding、Cluster mutation policy、Evidence Gate、action hash、target state、expiry 与 Connector availability。
- [x] 成功的 `批准并执行` 原子创建一个 Approval、一个 short-lived single-use Execution Grant 与一个 Connector Command；任一 precondition failure 不留下部分记录。
- [x] stale evidence、binding、target、expiry 或 action version 返回 `action_stale` 并要求重新审阅。
- [x] Connector 独立执行 exact-scope validation、preflight、execution lock 与 post-check。
- [x] text、Human Input、diagnostic model output、notification、`policy_grant` 与 `auto_execute` 在任何 Environment/risk 下都不能创建 mutation command。

门禁记录（T13）：Gateway Approval Module 为 `apps/aiops_k8s_gateway/approval.py`，公开 Interface 是 Approval Authority 管理、Workbench eligibility 投影与幂等 `approve_and_execute` transaction；HTTP Adapter 为 `approval_http.py`。Connector Command Module 在原公开 Interface 上增加同事务 restart queue，started mutation 不进入 T12 read retry；Connector worker 复用现有 CommandEnvelope/kubectl allowlist，独立校验 scope、Grant expiry 与 action hash，并固定执行 preflight、local execution lock、一次 restart 和 post-check。定向 selector 为 `tests/test_gateway_v1_approvals_contract.py`、`tests/test_gateway_connector_commands.py` 与 `tests/test_connector_command_worker.py`；直接 contract selector 为 `tests/test_gateway_v1_connectors_contract.py`、`tests/test_gateway_v1_incident_contract.py`、`tests/test_gateway_incidents.py`、`tests/test_gateway_diagnosis_delivery.py`、`tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_v1_resource_catalog_contract.py`、`tests/test_gateway_settings_policy.py`、`tests/test_architecture_boundaries.py` 与 `tests/test_k8s_guard.py`。legacy `policy_grant`/`auto_execute` mutation execution branch 已删除，保留的旧配置字段在 T24 前冻结但不再授予执行。`incident.py` 所属 Incident Module、公开 Workbench Interface 和 selector 均保持不变；任务开始时超大入口 `main.py` 为 5492 行，完成时低于该值，新生产文件均低于 800 行。后端定向与直接 contract 为 39 passed，Console Vitest 5 passed，TypeScript no-emit 与 production build 通过。

## T14 完成 bounded scale、revision rollback 与保守 outcome

**What to build:** 合资格 User 可以审批另外两种 V1 Deployment mutation，并在模糊执行结果或 post-check 失败时看到保守、可审计且不会自动重复 mutation 的状态。

**Blocked by:** T08 按稳定窗口 resolve 与 reopen Incident; T13 显式批准并执行 Deployment restart.

- [x] `scale_deployment` 冻结 current/target replica count 并执行配置 bounds。
- [x] `rollback_deployment` 只接受 approval 时已存在的 explicit revision，不接受相对“previous”。
- [x] shell、argv、Pod delete、apply、patch、exec、attach 与非 Deployment resource mutation 全部被拒绝。
- [x] Approval 可以包含一个 frozen conditional Rollback Plan，且仅在批准的 post-check condition 与 target assumptions 同时满足时触发。
- [x] changed、missing、unsafe 或 failed rollback 停在 `rollback_required`，不会临时生成新 inverse action。
- [x] started mutation 缺少 trustworthy terminal result 时进入 Unknown Outcome，绝不自动 retry；late result 只用于 reconciliation。
- [x] active mutation、conditional rollback 或 post-check 会延迟 Incident resolution，直到相关工作 terminal。
- [x] backend safety check 从公开 HTTP 边界验证 explicit Approval、single command 与 no automatic mutation retry。

门禁记录（T14）：Gateway Recommended Action/Evidence Gate 继续由 `apps/aiops_k8s_gateway/evidence_decisions.py` 拥有，Approval 与执行状态投影继续由 `approval.py` 拥有；Connector Command Module 的公开 Interface 为 typed mutation queue、Command Lease、Unknown Outcome reconciliation、late result reconciliation 与 Incident resolution blocker。Connector 的纯 Deployment mutation contract 位于 `apps/cluster_connector/deployment_mutations.py`，公开 Interface 为 frozen action/hash/bounds/Rollback Plan 校验与固定 envelope 构造；`command_worker.py` 只拥有 journal、execution lock、HTTP/kubectl 执行编排和 result handoff，`kubectl_executor.py` 仍是单一 Kubernetes argv allowlist/execution boundary。`connector_commands.py`、`evidence_decisions.py` 与 `kubectl_executor.py` 超过 500 行但低于 800 行，职责保持内聚，不为体量创建转发层；定向 selector 为 `tests/test_gateway_v1_approvals_contract.py`、`tests/test_gateway_connector_commands.py`、`tests/test_connector_command_worker.py`、`tests/test_k8s_guard.py` 与 `tests/test_gateway_incidents.py`，直接 contract selector 为 `tests/test_gateway_v1_connectors_contract.py`、`tests/test_gateway_v1_incident_contract.py`、`tests/test_gateway_diagnosis_delivery.py`、`tests/test_gateway_v1_auth_contract.py`、`tests/test_gateway_v1_resource_catalog_contract.py` 与 `tests/test_architecture_boundaries.py`。后端定向与直接 contract 为 43 passed；Console 只投影 Gateway execution state，generated OpenAPI types、Vitest 5 passed、TypeScript no-emit 与 production build 通过。最终全量 pytest 为 610 passed、9 failed；9 项均在未修改的 T12 HEAD `1b783e3` 用相同 selectors 复现，属于 frozen legacy `/api/*`、无 Enrollment credential 的旧 Connector registration 与已移除 diagnosis wrapper 测试，不属于 T14 回归。

## T15 发布不可变 Incident Report

**What to build:** Incident resolved 且所有 Investigation terminal 后，授权 User 可以编辑一个基于冻结事实的 Report draft，并人工发布不可变版本。

**Blocked by:** T08 按稳定窗口 resolve 与 reopen Incident; T11 生成 Evidence Step 与受 Gate 约束的 Recommended Action.

- [x] Gateway 只在 Incident resolved 且所有 Investigation terminal 后创建 Report draft。
- [x] draft 冻结 source Incident revision、included Investigation IDs、recorded facts、decision/action history 与 evidence references。
- [x] User 只能编辑 impact、root-cause explanation、resolution summary 与 follow-up narrative fields。
- [x] publish 必须是显式人类操作，每个 published version immutable。
- [x] reopened Incident 再次 resolved 后创建新 draft，不修改旧 publication。
- [x] Report 不暴露 model reasoning trace、run/session identity 或任意 HTML。
- [x] Console Report 页面清楚区分 draft、published version、immutable facts 与 editable narrative。

门禁记录（T15）：Incident Report Module 为 `apps/aiops_k8s_gateway/incident_reports.py`，公开 Interface 是按 Incident scope 读取/创建当前 draft、更新四个 narrative 字段和显式发布 immutable version；HTTP Adapter 为 `incident_report_http.py`。draft 以 source Incident revision 唯一，冻结全部 Investigation IDs、recorded facts、judgment/Recommended Action/Approval/execution history 与 evidence references；publication 另表保存 exact report JSON，并以 SQLite trigger 禁止 update/delete。Console Report 页面只通过生成的 OpenAPI types 与 TanStack Query 消费 Gateway state，复用现有 shadcn/ui primitive，并按 route lazy-load。定向 selector 为 `tests/test_gateway_incident_reports.py` 与 `tests/test_gateway_v1_report_contract.py`；直接 contract selector 为 `tests/test_gateway_incidents.py`、`tests/test_gateway_v1_incident_contract.py`、`tests/test_gateway_v1_approvals_contract.py`、`tests/test_gateway_diagnosis_delivery.py` 与 `tests/test_architecture_boundaries.py`。任务开始时超大 `main.py` 为 5478 行、`v1_store.py` 为 973 行，完成时分别为 5478 行与 973 行；新文件均低于 500 行。后端定向与直接 contract 为 28 passed；Console Vitest 5 passed、TypeScript no-emit 与 production build 通过。

## T16 通过独立 Notification Engine 投递领域事件

**What to build:** AIOps 的 Incident、Investigation、Approval、execution 与 Connector domain event 可以先成为 durable channel-neutral Notification Request，再由独立 Notification Engine 使用内置 Feishu presentation 投递；Engine 不可用不会回滚业务事实。

**Blocked by:** T02 落实 Kubernetes Service Identity 与网络边界; T10 持久回放 Investigation Event 并接收 Human Input; T14 完成 bounded scale、revision rollback 与保守 outcome.

- [x] Gateway 在业务 transaction 中持久化 versioned Notification Request，并独立重试 handoff 直到 Engine durable `202` acceptance。
- [x] Notification Engine 是独立单副本 process，使用自己的 `notification.db`，不读写 Gateway table。
- [x] Request 包含 event ID、typed event、occurred time、normalized severity、subject、scope、summary、validated facts 与 relative Console path。
- [x] Request 不包含 destination、recipient、template、credential、arbitrary JSON、raw log 或内部 run/session identity。
- [x] 规格定义的 Incident、Investigation、Approval、execution 与 Connector events 都会生成 idempotent request。
- [x] 内置 Feishu group-bot presentation 支持签名和 fake destination；业务状态不等待 Provider response。
- [x] Notification Engine 没有权限改变 Incident、Approval、Execution Grant 或 Connector Command。

门禁记录（T16）：共享 contract 为 `aiops/contracts/notification.py`，公开 Interface 是严格构造/验证 18 种 versioned channel-neutral Notification Request；Gateway Notification Request Module 为 `apps/aiops_k8s_gateway/notification_requests.py`，公开 Interface 是事务内幂等 enqueue、Connector presence reconcile 与 durable `202` handoff worker；独立 Notification Engine Module 为 `notification_service/requests.py`，公开 Interface 是 durable accept、idempotent conflict detection 与异步 delivery，HTTP Adapter 为 `notification_service/service_main.py`，Feishu presentation/signing 为 `notification_service/presentation.py`。任务开始时 `main.py` 5478 行、`incident.py` 710 行、`evidence_decisions.py` 542 行、`connector_commands.py` 592 行，完成时分别为 5478、729、551、614 行；前三个领域文件与 Connector Command 文件仍各自保持单一 owner，`main.py` 只增加装配，所有新文件低于 500 行。定向 selector 为 `tests/test_gateway_notification_requests.py`、`tests/test_notification_service.py`、`tests/test_notification_deployment.py`；直接 contract selector 为 `tests/test_gateway_incidents.py`、`tests/test_gateway_diagnosis_delivery.py`、`tests/test_gateway_v1_approvals_contract.py`、`tests/test_gateway_connector_commands.py`、`tests/test_k8s_manifests.py`、`tests/test_split_service_packaging.py`、`tests/test_docker_image_workflow.py` 与 `tests/test_architecture_boundaries.py`。

## T17 配置加密 Destination 与首条匹配 Route

**What to build:** Notification administrator 可以在 `/admin` 配置并测试由进程内 Apprise adapter 支持的 Feishu、DingTalk 与 SMTP Notification Destination，再用可预测的第一条匹配 Notification Route fan-out 或 suppress 请求。

**Blocked by:** T04 安全管理 User、Team 与 Role Binding; T16 通过独立 Notification Engine 投递领域事件.

- [x] pinned Apprise library 是唯一 Provider Adapter，Feishu group-bot、DingTalk group-robot 与 authenticated SMTP/TLS 各有经过验证的配置与 test delivery 流程；不部署 Apprise API。
- [x] webhook URL、signing secret 与 SMTP password 使用 authenticated encryption 存入 `notification.db`，API、log、audit 与 error 只显示 masked state。
- [x] 数据库加密密钥只由独立 Kubernetes Secret 以只读文件挂载，不写入 database/PVC、API、log 或 rendered configuration，也没有 environment variable fallback。
- [x] Notification Route 按 explicit priority 评估，第一条 enabled exact match 获胜，且必须有 final default route。
- [x] match 只支持 event、severity、Environment、Team 与 Service；route 可以 fan-out 或以 reason suppress。
- [x] duplicate destination 被 collapse，管理员可以 simulate request 并在 activation 前 test destination。
- [x] V1 不增加 provider-specific transport client、regex/script condition、generic webhook、personal message、SMS 或 voice provider。

门禁记录（T17）：Notification Configuration Module 为 `notification_service/configuration.py`，公开 Interface 是加密 Destination 创建/更新/测试、priority-first exact Route 创建/更新/simulation、Request routing 与 Apprise delivery；`notification_service/requests.py` 继续拥有 `notification.db` forward migration、事务内 Route result 与 fan-out Delivery 创建，内置 fake 只保留 T16 测试和升级前 unfinished work。Engine HTTP Adapter 为 `configuration_http.py`，Gateway 授权代理为 `notification_admin_http.py`，Console Module 为 `apps/aiops_console_web/src/admin/notification-admin.tsx`，浏览器只消费生成的 Gateway OpenAPI types。数据库 key 固定从独立只读 Secret 文件读取，无环境变量 fallback。任务开始时超大 `apps/aiops_k8s_gateway/main.py` 为 5478 行，完成时仍为 5478 行；新生产文件均低于 500 行。定向 selector 为 `tests/test_notification_configuration.py`、`tests/test_notification_service.py`、`tests/test_notification_deployment.py` 与 `tests/test_gateway_v1_notification_contract.py`；直接 contract selector 为 `tests/test_gateway_notification_requests.py`、`tests/test_gateway_v1_admin_contract.py`、`tests/test_architecture_boundaries.py`、`tests/test_k8s_manifests.py`、`tests/test_split_service_packaging.py` 与 `tests/test_docker_image_workflow.py`。后端定向与直接 contract 为 70 passed；Console Vitest 5 passed、TypeScript no-emit 与 production build 通过，Playwright 在 1440x1000 和 390x844 视口通过且无页面级横向溢出。

## T18 预览并冻结安全 Notification Template

**What to build:** Notification administrator 可以复制内置 Notification Template、修改安全展示字段并在 preview/test 后启用；每次 Delivery 保留实际使用的 presentation 历史。

**Blocked by:** T17 配置加密 Destination 与首条匹配 Route.

- [x] 每种 supported event/provider combination 都有内置 template。
- [x] custom template 只允许 documented `{{field}}` variable 与 title、Markdown body、color、button label；SMTP 另有 subject。
- [x] template 不支持 loop、condition、function、script、arbitrary HTML、recipient、destination 或 credential。
- [x] SMTP Markdown 产生 sanitized HTML 与 plain-text alternative。
- [x] draft 必须成功 preview 或 test delivery 后才能 enabled。
- [x] Route 只能选择 enabled 且 compatible template，否则使用 built-in template。
- [x] 每个 Notification Delivery 冻结 exact template version 与 rendered content，后续 edit 不改变 history 或 retry。

门禁记录（T18）：Notification Template Module 为 `notification_service/templates.py`，公开 Interface 是 built-in/custom template list/copy、版本化 edit、preview/test validation、enabled version selection 与 restricted render；`notification_service/configuration.py` 只在 Route validation/routing 和 compatible Destination test delivery 处消费该 Interface。`notification_service/requests.py` 通过 forward migration 增加 template/version/presentation freeze，并在 Request acceptance transaction 内写入每个 Delivery 的 exact rendered content；worker 和 retry 只读取冻结结果。Engine/Gateway HTTP Adapter 继续为 `configuration_http.py` 与 `notification_admin_http.py`，Console Template Module 为 `apps/aiops_console_web/src/admin/notification-template-admin.tsx`，Gateway OpenAPI 是浏览器唯一 contract。新生产文件均低于 500 行，既有超大 `apps/aiops_k8s_gateway/main.py` 未增长。定向 selector 为 `tests/test_notification_configuration.py`、`tests/test_notification_service.py` 与 `tests/test_gateway_v1_notification_contract.py`；直接 contract selector 为 `tests/test_gateway_notification_requests.py`、`tests/test_architecture_boundaries.py`、`tests/test_k8s_manifests.py`、`tests/test_notification_deployment.py`、`tests/test_split_service_packaging.py` 与 `tests/test_docker_image_workflow.py`。后端定向与直接 contract 为 75 passed；Console Vitest 5 passed、TypeScript no-emit 与 production build 通过。

## T19 用 quiet hours、digest 与 Silence 控制噪声

**What to build:** Notification administrator 可以为 Destination 配置常规噪声控制，并在确有需要时创建有期限、可审计的 Notification Silence。

**Blocked by:** T17 配置加密 Destination 与首条匹配 Route.

- [x] Destination 可以配置 timezone、quiet hours、hourly limit 与 digest interval。
- [x] lower-severity request 可以 durable defer、digest 或 limit，且每个结果仍可查询。
- [x] `critical` 默认绕过 quiet hours 与 hourly limit。
- [x] scoped Notification Silence 可以覆盖 `critical`，但必须 fresh authentication、explicit reason、bounded expiry 与 audit。
- [x] 同一 event ID 对同一 destination 最多创建一个 Notification Delivery。
- [x] V1 不增加 acknowledgment escalation 或 periodic reminder。

门禁记录（T19）：Notification Noise Control Module 为 `notification_service/noise_controls.py`，公开 Interface 是 Destination noise policy 读取/更新、bounded scoped Silence 创建/查询与基于 Delivery owner 提供的滚动计数判定策略；`notification_service/requests.py` 继续以 `NotificationStore` 公开 Interface 独占 Notification Request/Delivery 持久化、query result 与 worker，在放行前复核 hourly quota，并把 quiet hours、hourly limit、digest 和 Silence 结果冻结到每条 `(event_id, destination)` 唯一 Delivery。Engine/Gateway HTTP Adapter 为 `service_main.py`、`configuration_http.py` 与 `notification_admin_http.py`，Gateway 复用 fresh-auth 与结构化 admin audit；Console Module 为 `apps/aiops_console_web/src/admin/notification-noise-admin.tsx`。任务开始时 `notification_service/configuration.py` 为 488 行、`notification_service/requests.py` 为 372 行，完成时分别为 461 行与 529 行；`requests.py` 保持单一 Delivery owner 职责且本票公开 Interface/定向 selector 已在修改前确认，新生产文件均低于 500 行。定向 selector 为 `tests/test_notification_noise_controls.py`、`tests/test_notification_configuration.py`、`tests/test_notification_service.py` 与 `tests/test_gateway_v1_notification_contract.py`；直接 contract selector 为 `tests/test_gateway_notification_requests.py`、`tests/test_architecture_boundaries.py`、`tests/test_k8s_manifests.py`、`tests/test_notification_deployment.py`、`tests/test_split_service_packaging.py` 与 `tests/test_docker_image_workflow.py`。后端定向与直接 contract 为 81 passed；Console Vitest 5 passed、TypeScript no-emit 与 production build 通过，Playwright 在 1440x1000 和 390x844 视口通过且无页面级横向溢出。最终全量 pytest 为 652 passed、9 failed；9 项均在未修改的 T18 固定点 `47d517e` 用相同 selector 复现，属于冻结 legacy Connector registration 未携带 Enrollment credential 与已移除 diagnosis wrapper，不是 T19 回归。

## T20 处理 retry、dead-letter 与 redelivery

**What to build:** Notification administrator 可以看到失败 Delivery 的 durable retry 状态，修复 Destination 后手工 redeliver dead-letter，而不会丢失或误称 exactly-once。

**Blocked by:** T17 配置加密 Destination 与首条匹配 Route.

- [ ] worker 使用 lease claim due Notification Delivery，并在 process restart 后继续未完成工作。
- [ ] network error、timeout、`429` 与 `5xx` 使用 bounded exponential backoff，并遵守 `Retry-After`。
- [ ] non-retryable `4xx` 与 exhausted retry 进入可查询 dead-letter。
- [ ] 管理员修复 configuration 或 credential 后可以显式 redeliver，原失败 history 保留。
- [ ] event ID 与 destination uniqueness 防止 duplicate Delivery record。
- [ ] UI 与文档明确 delivery 是 at least once；Provider 接受但 response 丢失时允许 rare duplicate transport message。
- [ ] Apprise transport error 被归一化到 retryable/non-retryable result，Notification Engine 补充 bounded-label retry/dead-letter metrics、PrometheusRule 与 request/correlation JSON log。

## T21 独立构建并按 digest 提升 Console

**What to build:** Release operator 可以独立构建、验证和手工提升 `aiops-console` image，并通过单一 HTTPS origin 访问 Console、Gateway API、authentication 与 SSE。

**Blocked by:** T01 保存 Console 基线并建立 greenfield shell.

- [ ] Monorepo pull request 的 path-aware Console job 从 workspace lockfile 安装并运行 TypeScript check 与 production build。
- [ ] main build 发布带 immutable monorepo commit identity 的 `aiops-console` image，release manifest 选择 verified digest。
- [ ] production promotion 是显式人工操作，不自动部署 main build；mutable tag 不作为 release identity。
- [ ] edge route 将 static path 交给 Console，将 `/api/v1/*` 与 `/auth/*` 交给 Gateway。
- [ ] Console 使用 relative URL，同一 origin 保留 first-party Cookie 与 CSRF 语义。
- [ ] SSE route 禁用 proxy buffering 并支持 long-lived authenticated read。
- [ ] 当前与上一已提升版本的 Console/Gateway artifact 通过 N/N-1 OpenAPI contract compatibility；不兼容变更使用新 API version。
- [ ] Gateway legacy static serving 在 T24 replacement acceptance 前保持冻结，本票不提前删除。

## T22 观察跨服务 durable work

**What to build:** Platform operator 可以通过 Prometheus 与结构化日志识别 unavailable service、stalled durable work、Connector heartbeat age、Unknown Outcome、dead-letter 与 SQLite/storage 问题。

**Blocked by:** T14 完成 bounded scale、revision rollback 与保守 outcome.

- [ ] 每个服务暴露 bounded-label HTTP RED metrics 与适合其 durable work 的 queue depth/oldest age metrics。
- [ ] metrics 先覆盖 Connector heartbeat、Diagnosis outcome/duration、Command Lease、Unknown Outcome、SSE connection、SQLite error 与 storage pressure；T20 在同一公开指标约束下补充 Notification retry/dead-letter。
- [ ] metric label 不包含 unbounded Incident、User、Command 或 Delivery identity。
- [ ] structured JSON log 先跨 Gateway、Diagnosis 与 Connector 传播 request ID 与 correlation ID；T20 将相同约束延伸到 Notification Engine。
- [ ] log 不包含 credential、session material、raw evidence 或 full request body。
- [ ] 小型 PrometheusRule 集先覆盖 control-plane unavailable、stalled work、Unknown Outcome 与 storage pressure；Notification dead-letter rule 由 T20 在 delivery state 可用时补充。
- [ ] V1 不部署 OpenTelemetry SDK、Collector 或 tracing backend。

## T23 落实 V1 数据库所有权与固定保留策略

**What to build:** Platform operator 可以明确每个 stateful process 的唯一数据库与固定 cleanup 行为，使 V1 state 在 restart 后持久且不会因 technical churn 无界增长。

**Blocked by:** T12 通过长轮询执行持久只读 Connector Command; T15 发布不可变 Incident Report; T20 处理 retry、dead-letter 与 redelivery; T22 观察跨服务 durable work.

- [ ] V1 路径只使用 Gateway 的 `gateway.db`、Diagnosis 的 `diagnosis.db`、Notification Engine 的 `notification.db` 与每个 Connector 的 local `connector.db`。
- [ ] 产品部署保持 Kubernetes-only；每个 stateful control-plane process 使用一个 active replica 与自己的 PVC，Docker Compose 只作为 development/image smoke。
- [ ] process 不访问其他 process table；process 内需要原子性的模块共享同一 transaction boundary。
- [ ] 每个 store 使用 explicit forward migration、foreign key、constraint 与 application-generated identifier，不添加 dual SQLite/PostgreSQL repository abstraction。
- [ ] Gateway governance history 不自动删除；raw Prometheus sample 与 Loki log 不复制进这些数据库。
- [ ] acknowledged Connector journal 与已由 Gateway 安全保留结果/evidence reference 的 terminal Diagnosis internal data 在 30 天后 cleanup；terminal Notification Request、Delivery 与 rendered content 在 90 天后 cleanup。
- [ ] heartbeat 覆盖 current state，只把 transition 留作 durable history；expired Session、Lease 与 lock 及时 cleanup。
- [ ] unresolved dead-letter 与 Unknown Outcome 永不自动删除，cleanup 和 storage pressure 可被观察。
- [ ] 本票不实现 backup；PVC loss risk 与 PostgreSQL 后置恢复决策保持显式。

## T24 退役 legacy contract 并完成模型阶段验收

**What to build:** 新 V1 主链被接受后，维护者可以 contract 掉旧 Console product contract、Gateway static serving、Agent Run/panel surface 与模块级数据库，使系统只保留一个公开模型和一个 Console 路径。

**Blocked by:** T18 预览并冻结安全 Notification Template; T19 用 quiet hours、digest 与 Silence 控制噪声; T21 独立构建并按 digest 提升 Console; T23 落实 V1 数据库所有权与固定保留策略.

- [ ] login、Incident list、Workbench、Incident Report 与 permission-gated `/admin` 的 V1 replacement path 均已验收。
- [ ] frozen unversioned `/api/*`、legacy Agent Run/Diagnosis Session/generic panel contract 与 Gateway static asset path 被删除。
- [ ] 没有 V1 caller 继续使用 module-level database；V1 所需 retained record 在 owner store 可用后才删除 legacy state path。
- [ ] 不保留 legacy UI mode、compatibility component、parallel route tree、permanent dual-write 或第二套客户端 Investigation state machine。
- [ ] backend safety gate 通过 authorization、explicit Approval、no automatic mutation/retry 与 representative OpenAPI response/error/SSE validation。
- [ ] Console 通过 TypeScript no-emit check 与 production build，固定 OpenAPI snapshot 的 generated types 参与编译。
- [ ] 负向 deterministic HTTP smoke 使用 fake AI 覆盖 Alert Signal → Incident → Investigation → Recommended Action，并证明 Approval 前没有 Connector Command。
- [ ] 正向 deterministic HTTP smoke 使用 fake AI、fake Connector 与 fake Notification Destination 覆盖 Approval → Connector Command → result → Incident Report → Notification Delivery。
- [ ] Diagnosis Request、Connector Command 与 Notification Delivery 各有一个关闭并重开 owner SQLite store 的最小 restart-recovery check，证明 accepted unfinished work 恢复且不重复。
- [ ] 本阶段不增加 real Provider/AI、browser matrix、real Kubernetes mutation、Kubernetes-level fault injection、production release、HA、backup 或 PostgreSQL acceptance。
