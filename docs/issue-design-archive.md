# Issue 设计归档

最后对齐日期：2026-06-17

本文保留历史 Multica issue 中已经沉淀为长期事实的产品和架构决策。它不是任务状态台账。

## AIO-73 架构收敛

决策：

- 短期使用 Hermes 作为 diagnosis brain。
- Gateway/control-plane 是权威 control boundary。
- Connector 负责集群内 Kubernetes execution。
- MCP services 负责 Prometheus、Loki 和 Topology evidence。
- P0 保持 read-only，mutation execution 进入后续 P2。
- 当前阶段不做 Helm。
- 当前阶段不做 Brain Provider abstraction。
- Approval authority 收敛到内部 Gateway/control-plane service。
- Feishu 保持 notification-only。

P0 顺序：

1. AIO-74：Gateway Alertmanager webhook ingress。
2. AIO-75：Hermes diagnosis session orchestrator。
3. AIO-76：Gateway-to-Connector read-only K8s execution。
4. AIO-77：fixed-digest end-to-end alert-to-diagnosis smoke。

## AIO-80 内部 Approval Service

决策：

- Approval request 状态由 Gateway/control-plane 拥有。
- 最小状态机：`pending -> approved | rejected | expired | cancelled`。
- Reject 必须填写 reason。
- RBAC、scope、expiry、idempotency 和 terminal-state protection 都是服务端职责。
- Feishu 可以通知和 deep-link 到 Console，但不能 approve、reject 或 cancel。

当前实现锚点：

- `apps/aiops_k8s_gateway/approval_service.py`
- `apps/aiops_k8s_gateway/main.py` 中的 `/api/approval-requests` routes

## AIO-86 Notification Center

决策：

- Notification delivery 由 Gateway 拥有并记录。
- Feishu 是 notification-only channel。
- Notification failure 应进入 failed/dead-letter delivery record，不能反向改变 approval 状态。
- Feishu card 不能携带 `approve`、`reject`、`approval_decision` 或等价的状态变更 action。

当前实现锚点：

- `apps/aiops_k8s_gateway/notification_center.py`
- [Notification Center 与 Feishu](notification-center-feishu.md)

## AIO-87 Console V1

决策：

- Browser 只有一个 data boundary：Gateway/control-plane API。
- Console V1 覆盖 incident history、incident detail、Approval Center、costs、Grafana compatibility 和最小 management entry points。
- Frontend authorization check 只服务体验，Gateway 仍是权威。
- Grafana 是 embed/display compatibility，不是 AIOps-managed observability stack。

当前交接文档：

- [Console V1 契约](aiops-console-v1-contract.md)
- `apps/aiops_console`

## AIO-95 Diagnosis Writeback

决策：

- P0 接受 Hermes HTTP export 作为 smoke artifact of record。
- 生产 history 需要 Gateway-durable diagnosis artifact。
- Writeback 没有 HMAC signature 时必须 fail closed。
- 如果 Gateway writeback 失败，Hermes export 仍可用，并记录 writeback failure。

当前实现锚点：

- `aiops/contracts/writeback_auth.py`
- `apps/aiops_k8s_gateway/diagnosis_writeback.py`
- `tests/test_hermes_diagnosis_service.py`
- `tests/test_gateway_alertmanager_webhook.py`

## 已废弃的旧决策

以下决策不再代表当前方向：

- Feishu-native approval 作为主审批路径。
- Feishu text approval 或 card button 作为权威 approval transition。
- all-in-one Hermes/Feishu SRE Agent 作为主运行形态。
- `docs/development-progress.md` 和 `docs/TODD.md` 作为当前状态来源。
- `docs/superpowers/*` 作为活跃 implementation spec。
