# 当前 AIOps 架构

最后对齐日期：2026-06-17

## 事实来源

当前架构基于以下来源整理：

- AIO-73：Hermes 主导诊断 + Gateway control-plane + Connector execution 的架构收敛。
- AIO-74 至 AIO-77：P0 Gateway webhook、Hermes diagnosis session、Gateway 到 Connector 只读执行、端到端 smoke 验收。
- AIO-80、AIO-84、AIO-85、AIO-86、AIO-87：内部 Approval Service、RBAC、CMDB ownership、Notification Center 和 Console contract。
- AIO-93 至 AIO-96：Topology runtime、K8s selector 精确度、diagnosis writeback、root-cause precision 的后续缺口。
- 当前代码：`apps/`、`aiops/`、`deploy/k8s/`、`hermes/`、`tests/`。

任务状态、验收结论和剩余风险仍以 Multica issue 为准。

## 架构摘要

AIOps 当前是 split-service diagnostic control plane：

- Alertmanager 将告警发送到 split `aiops-gateway`。
- Gateway/control-plane 拥有 ingress、incident/session 状态、RBAC、内部审批、通知路由、audit、K8s command routing 和 diagnosis writeback。
- Hermes 负责 diagnosis orchestration：选择工具、收集证据、生成结构化诊断和 action proposal。
- Connector 运行在集群内，执行 Gateway 授权的 Kubernetes command envelope。默认部署 profile 是 read-only。
- MCP services 暴露 Prometheus、Loki 和 Topology evidence 边界。
- Console 只通过 Gateway API 读写。
- Feishu 是 notification-only channel。它可以承载摘要和 Console 链接，但不拥有 approval 状态，不能 approve/reject。

## 非目标

- 当前交付路径不做 Helm chart。
- P0/P1 不做 Codex、pi 或 Brain Provider 抽象。
- P0 不做生产 mutation execution。
- Browser 不直连 Hermes、Connector、MCP、Prometheus、Loki 或 Feishu approval API。
- Feishu 原生审批不是权威审批链路。
- bundled dev Prometheus/Loki 不代表生产级 observability backend。

## 进程边界

| 边界 | 代码位置 | 职责 |
| --- | --- | --- |
| Gateway/control-plane | `apps/aiops_k8s_gateway` | Alert ingress、incident/session、RBAC、approval service、audit、notification、Connector routing、diagnosis writeback。 |
| Hermes diagnosis | `hermes/`, `hermes/service_main.py` | Diagnosis session orchestration、evidence planning、structured diagnosis export/writeback。 |
| Cluster Connector | `apps/cluster_connector` | 集群内执行已授权的 read command envelope。 |
| Prometheus MCP | `apps/mcp_prometheus` | Prometheus query facade 和 evidence envelope。 |
| Loki MCP | `apps/mcp_loki` | Loki query facade 和 evidence envelope。 |
| Topology MCP | `apps/mcp_topology` | Service topology query facade。 |
| Console static slices | `apps/aiops_console` | 基于 Gateway-only contract 的 V1 UI 静态 vertical slice。 |
| Shared contracts/domain | `aiops/contracts`, `aiops/domain`, `aiops/k8s` | 稳定 envelope、error、evidence ref、writeback auth、identity、topology、incident、command model。 |
| Legacy compatibility | `hooks/`, `runtime/`, `toolsets/` | V1 迁移期兼容层；新领域逻辑默认不继续沉到这里。 |

## 主流程

### Alert To Diagnosis

1. Alertmanager 调用 Gateway `POST /webhooks/alertmanager`。
2. Gateway 校验 payload/HMAC，创建或复用 incident/session，写 timeline/audit event，并触发 Hermes。
3. Hermes 在可用时收集 Prometheus、Loki、K8s 和 Topology evidence。
4. Hermes 导出结构化 diagnosis 和 action proposal。
5. Hermes 通过受保护的 `POST /diagnosis/writeback` 将诊断 artifact 写回 Gateway。
6. Gateway incident view 和 Console 消费 durable incident artifact。

### Approval

1. Hermes 或 Gateway 在存在 remediation candidate 时创建 action proposal。
2. Gateway internal Approval Service 通过 `/api/approval-requests` 创建 approval request。
3. Gateway 按配置发送 Feishu notification，附内部 Console 链接。
4. Approver 在内部 Approval Center API approve/reject。
5. Gateway 执行 RBAC、scope、status、expiry 校验并写 audit。
6. 已 approve 的 request 未来可作为 P2 mutation work 的 execution grant；P0/P1 diagnosis path 不执行 mutation。

### Console

1. Browser 通过 Gateway 认证。
2. Browser 通过 Gateway `/api/*` 获取 incident、diagnosis、evidence、approval、cost、Grafana panel metadata 和 audit。
3. Browser 永远不直接访问 Hermes、Connector、MCP、Prometheus、Loki 或 Feishu。

## 部署状态

Native Kubernetes YAML 位于 `deploy/k8s/`。当前 overlay：

- `overlays/dev-bundled`：AIOps + bundled dev Prometheus/Loki compatibility backend、`payment-api` 和 synthetic Loki logs。
- `overlays/dev-external`：AIOps 接已有 Prometheus/Loki endpoint。
- `overlays/dev-disabled`：observability URL 为空，验证受控降级。
- `overlays/rc-bundled-digest`：固定 immutable digest 的 release-candidate profile。
- `overlays/dev-remediation-rbac`：受控 remediation 测试的 opt-in mutation RBAC；不属于默认验证。

默认 Connector RBAC 是 read-only。

## 已接受的 P0 结果

P0 已按 fixed-digest end-to-end smoke 接受，结论允许 partial evidence：

- Alertmanager -> split Gateway -> incident/session -> Hermes -> Prometheus/Loki/K8s evidence -> structured diagnosis export 可用。
- Mutation 未执行。
- Hermes HTTP export 被接受为本次 smoke 的 artifact of record；durable Gateway writeback 已作为后续能力进入代码和测试。
- Topology evidence 在 runtime/data 未就绪时可以是 skipped 或 partial。

## 未收口风险

| 风险 | 跟踪 |
| --- | --- |
| Topology runtime/data availability 仍可能导致 skipped 或 partial evidence。 | AIO-93 |
| K8s selector precision 依赖当前 label convention，例如 `app.kubernetes.io/name`。 | AIO-94 |
| 生产 incident history 需要 durable diagnosis writeback。 | AIO-95 |
| Root-cause precision 还需要更强的 evidence-to-cause classification。 | AIO-96 |
| Approval Center 依赖内部 Approval Service、RBAC、CMDB ownership 和 notification contract。 | AIO-80, AIO-84, AIO-85, AIO-86 |
