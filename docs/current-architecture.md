# 当前 AIOps 架构

最后对齐日期：2026-07-11

## 架构摘要

AIOps 当前是面向 Kubernetes 告警诊断和受控运维的 source monorepo，运行时仍是 split-service control plane 与独立 Console artifact：

- `apps/aiops_k8s_gateway` 是唯一外部入口，负责 Alertmanager ingress、incident/session、认证、RBAC、内部审批、通知、审计、Connector routing 和 diagnosis writeback。
- `diagnosis_service/` 负责诊断编排、证据收集、结构化 diagnosis 和 action proposal。
- `apps/cluster_connector` 运行在集群内，通过主动长轮询领取 Gateway-owned durable Connector Command，并以本地 `connector.db` journal 执行有界 Kubernetes read 或显式批准的 Deployment restart、bounded scale 与 explicit revision rollback；默认部署 profile 是 read-only。
- `apps/mcp_prometheus`、`apps/mcp_loki`、`apps/mcp_topology` 分别提供 Prometheus、Loki 和 Topology evidence 边界。
- `aiops/contracts`、`aiops/domain`、`aiops/k8s` 保存共享协议、领域模型和 Kubernetes envelope。
- `runtime/` 保存后端 smoke/worker；`toolsets/` 保存当前后端仍使用的本地工具实现。
- `apps/aiops_console_web` 保存 Console Web source workspace。Console 仍独立构建和部署；Gateway 当前的可选 `AIOPS_CONSOLE_DIST_DIR` 静态挂载仅是待 V1 replacement acceptance 后删除的 legacy path。

Gateway、Diagnosis 与三个 MCP 进程在 Kubernetes 中使用各自的 ServiceAccount 和 `aiops-internal` audience 短期 projected token。内部 HTTP 接收端通过 TokenReview 认证并按 namespace/ServiceAccount 授权；Diagnosis 与 MCP ClusterIP ingress 由 NetworkPolicy 限制。浏览器 Session、Alertmanager ingress 与跨 Cluster Connector 继续使用各自独立的外部身份机制。

## 非目标

- Gateway image 不捆绑 Console source 或 build output。
- 当前部署路径不做 Helm chart。
- 浏览器不得直连 diagnosis service、Connector、MCP、Prometheus、Loki 或 Feishu API。
- Feishu 是 notification-only channel，不能推进 approval 状态。
- 默认 P0/P1 diagnosis profile 不执行 Kubernetes mutation。

## 主流程

### Alert To Diagnosis

1. Alertmanager 调用 Gateway `POST /webhooks/alertmanager`。
2. Gateway 校验 payload/token，在 `gateway.db` 的同一事务中创建或复用 Incident，并为首个 Investigation 持久化 Diagnosis Request。
3. Gateway 以退避重试把 Request 交给 Diagnosis；Diagnosis 在 `diagnosis.db` 中幂等持久化 Diagnosis Job 后才返回 `202 Accepted`。
4. Diagnosis Job 独立收集 Prometheus、Loki、K8s 和 Topology evidence；单个 evidence source 失败形成 partial 或 needs-human 结果。
5. Diagnosis 先持久化完成 artifact，再独立重试受保护的 `POST /diagnosis/writeback`，writeback 失败不会重跑 Job。
6. Gateway 在确认 writeback 前，把 Diagnosis observation canonicalize 为 Gateway-owned Evidence Step，并在同一事务中持久化 diagnosis output、tool activity、Evidence Step change、judgment、Recommended Action 与 lifecycle transition；MCP 与 Connector observation 不拥有产品状态。
7. Gateway 的确定性 Evidence Gate 按真实 Resource Binding、scope、freshness、reference integrity 与 action-specific requirement 决定 mutation 是否可审批；incomplete evidence 仍保留 judgment 和 next-evidence guidance，但不会产生 approvable mutation。
8. Workbench snapshot 返回 Evidence Step、judgment、immutable Recommended Action 和持久 event cursor；Console 通过 `/api/v1/investigations/{id}/events` 分页回放，并以 SSE `Last-Event-ID` 从 cursor 恢复实时进展。
9. SRE 提交的 assertion、correction 和 retraction 只追加 immutable Human Input Event，不成为 Evidence 或执行授权；影响依赖判断的 correction/retraction 会使 judgment 和 Recommended Action stale。pause、human-led takeover、terminate 与 reinvestigate 同样产生权限受控的 lifecycle Event。

Gateway 独立记录每个 Alert Signal 的 firing/recovered 状态；全部 Signal 恢复后以 Recovery Observation 启动稳定窗口，窗口完成后 resolve Incident，配置的 reopen 窗口内相关复发继续归入原 Incident。

Gateway 与 Diagnosis 分别挂载 `aiops-gateway-data` 和 `aiops-diagnosis-data` PVC，各自只拥有 `gateway.db` 与 `diagnosis.db`。

### Connector Command

1. Gateway 在 `gateway.db` 中先持久化 typed `get_resource` 或受 Execution Grant 约束的 `restart_deployment`、`scale_deployment`、`rollback_deployment` Connector Command；Connector 通过带 Enrollment credential 的 HTTPS 长轮询，只能领取自身 identity 与 Cluster 的命令。
2. Gateway 在同一事务中授予短期 Command Lease。Connector 先把命令写入本地 `connector.db`，再报告 start；只有收到 acknowledgement 后才把 typed parameters 转为受 allowlist 约束的 kubectl argv 并执行。
3. Connector 在本地持久化 terminal result 后再上报。相同结果可幂等重放，冲突结果被拒绝并审计，过期 lease 的 late result 只用于 reconciliation。
4. 未 start 的过期 lease 可重新领取；已 start 的 read 最多尝试三次，已 start 的 mutation 不会自动重领，缺少可信 terminal result 时由 Gateway 标记 Unknown Outcome。Connector 重启时先重发未确认 terminal result，再继续轮询。
5. Gateway 不向 Connector 发起入站连接；Connector 独立校验 Cluster、namespace、action、Deployment、typed parameters、Execution Grant expiry 与 frozen action hash。scale 执行 Gateway 装配配置的 `AIOPS_SCALE_MIN_REPLICAS`/`AIOPS_SCALE_MAX_REPLICAS` bounds，且 Connector 保留 0..20 硬上限；rollback 必须使用 evidence 中已存在的 explicit revision；所有 mutation 固定执行 preflight、scope lock、一次 mutation 和 post-check，不接受 shell 或自由 argv。
6. Conditional Rollback Plan 必须随 Approval 冻结，只有批准的 post-check failure 与 target assumptions 同时成立才执行 exact inverse；missing、changed、unsafe 或 failed plan 停在 `rollback_required`。未终结 mutation 会阻止 Incident resolution。

Connector 使用独立 `aiops-connector-data` PVC 保存 `connector.db`。Console 的 Cluster 管理视图只投影真实 heartbeat、pending read command 数量和最后 command 结果。

### Approval

1. Diagnosis 只提交 Recommended Action；Gateway canonicalize 并冻结 target、typed parameters、Evidence Steps、safeguards、Rollback Plan 与 action hash。
2. Platform Administrator 通过 fresh-auth gate 创建同时覆盖 Environment 与真实资源的 Approval Authority，本身不获得隐式审批权。
3. Workbench 只向当前 Authority 覆盖目标的 User 展示 `批准并执行`。Human Input、模型输出、通知和 legacy `policy_grant`/`auto_execute` 都不能进入该命令。
4. Gateway 在一个 transaction 内重新验证 Authority、Resource Binding revision、Cluster mutation policy、Evidence Gate、action hash、target、expiry 与 Connector heartbeat。
5. 验证成功后原子持久化 Approval、短期单次 Execution Grant 和唯一 Connector Command；任何前置失败都不留下这三类部分记录。Workbench 投影 Connector Command 的执行状态、Unknown Outcome 与 rollback result。

未版本化 legacy Approval API 在 T24 前继续冻结，但不参与 V1 Incident/Recommended Action 执行路径。

### Incident Report

1. Gateway 只在 Incident 已 resolved 且全部 Investigation terminal 后，于首次授权读取时在 `gateway.db` 创建当前 resolution cycle 的 Report draft。
2. draft 按 source Incident revision 唯一，冻结全部 included Investigation IDs、recorded facts、judgment/Recommended Action/Approval/execution history 与 evidence references；不包含模型 reasoning trace、run/session identity、raw execution output 或 HTML。
3. User 只能修改 impact、root-cause explanation、resolution summary 与 follow-up narrative。显式 publish 把 exact draft 保存为不可更新、不可删除的 immutable version。
4. Incident reopen 后旧 publication 保持不变；再次 resolved 且所有 Investigation terminal 后，新的 source revision 创建下一份 draft。

Console 的 `/incidents/:incidentId/report` 使用 Gateway OpenAPI generated types 和 TanStack Query，分别呈现 editable narrative、immutable facts 与 published version history。

## 部署状态

Native Kubernetes YAML 位于 `deploy/k8s/`。当前 overlay：

- `overlays/dev-bundled`：AIOps + bundled dev Prometheus/Loki compatibility backend、`payment-api` 和 synthetic Loki logs。
- `overlays/dev-external`：AIOps 接已有 Prometheus/Loki endpoint。
- `overlays/dev-disabled`：observability URL 为空，验证受控降级。
- `overlays/rc-bundled-digest`：固定 immutable digest 的 release-candidate profile。
- `overlays/dev-remediation-rbac`：受控 remediation 测试的 opt-in mutation RBAC，不属于默认验证。

默认 Connector RBAC 是 read-only。

## 当前风险

| 风险 | 说明 |
| --- | --- |
| Topology runtime/data availability | Topology evidence 可能是 skipped 或 partial。 |
| K8s selector precision | 依赖当前 label convention，例如 `app.kubernetes.io/name`。 |
| Root-cause precision | 仍需要更强的 evidence-to-cause classification。 |
| Mutation execution | 只在显式 opt-in RBAC 和 Gateway approval/execution guardrail 下验证。 |
