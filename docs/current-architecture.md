# 当前 AIOps 架构

最后对齐日期：2026-07-11

## 架构摘要

AIOps 当前是面向 Kubernetes 告警诊断和受控运维的 source monorepo，运行时仍是 split-service control plane 与独立 Console artifact：

- `apps/aiops_k8s_gateway` 是唯一外部入口，负责 Alertmanager ingress、incident/session、认证、RBAC、内部审批、通知、审计、Connector routing 和 diagnosis writeback。
- `diagnosis_service/` 负责诊断编排、证据收集、结构化 diagnosis 和 action proposal。
- `apps/cluster_connector` 运行在集群内，执行 Gateway 授权的 Kubernetes command envelope；默认部署 profile 是 read-only。
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
2. Gateway 校验 payload/token，创建或复用 incident/session，写 timeline/audit event。
3. Gateway 通过 `AIOPS_DIAGNOSIS_URL` + `AIOPS_DIAGNOSIS_PATH` 触发 diagnosis service。
4. Diagnosis service 收集 Prometheus、Loki、K8s 和 Topology evidence。
5. Diagnosis service 生成 structured diagnosis 和 action proposal。
6. Diagnosis service 通过受保护的 `POST /diagnosis/writeback` 将 artifact 写回 Gateway。
7. Gateway incident API 和 Console Web 消费 durable incident artifact。

Gateway 独立记录每个 Alert Signal 的 firing/recovered 状态；全部 Signal 恢复后以 Recovery Observation 启动稳定窗口，窗口完成后 resolve Incident，配置的 reopen 窗口内相关复发继续归入原 Incident。

### Approval

1. Diagnosis service 或 Gateway 创建 action proposal。
2. Gateway internal Approval Service 通过 `/api/approval-requests` 创建 approval request。
3. Gateway 按配置发送 Feishu notification，附内部 Console 链接。
4. Approver 在内部 Console/Gateway API approve 或 reject。
5. Gateway 执行 RBAC、scope、status、expiry 校验并写 audit。
6. 已 approve 的 request 才能成为后续 mutation execution grant。

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
