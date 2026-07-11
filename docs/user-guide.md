# AIOps 后端用户手册

最后对齐日期：2026-07-09

## 当前系统能力

AIOps 接收 Alertmanager 告警，创建或复用 incident，触发 diagnosis service，收集 Prometheus/Loki/K8s/Topology evidence，并通过 Gateway-controlled API 暴露结构化诊断 artifact。

当前边界：

- 支持只读 diagnosis。
- 默认 Kubernetes execution 是 read-only。
- Action proposal 可以要求 approval，但默认 profile 不执行 mutation。
- Feishu 只负责通知和跳转链接。
- Approval 必须在内部 Console/Gateway API 完成，不能在 Feishu 完成。

## 部署入口

```bash
kubectl apply -k deploy/k8s/overlays/dev-bundled
kubectl apply -k deploy/k8s/overlays/dev-external
kubectl apply -k deploy/k8s/overlays/dev-disabled
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

完整部署、镜像、Secret、profile 切换和 smoke 命令见 [deploy/k8s/README.md](../deploy/k8s/README.md)。

## 常用 Gateway Endpoint

| Endpoint | 用途 |
| --- | --- |
| `GET /healthz` | Gateway health。 |
| `GET /readyz` | Gateway readiness 和 connector count。 |
| `POST /webhooks/alertmanager` | Gateway Alertmanager ingress。 |
| `POST /diagnosis/writeback` | 受保护的 diagnosis artifact writeback。 |
| `GET /incidents/{incident_id}` | 受保护 lower-level incident view。 |
| `POST /auth/login` | Gateway auth/session 入口。 |
| `GET /auth/me` | 当前 actor 和 permission。 |
| `POST /api/v1/connectors/register` | Connector 使用 Enrollment credential 主动注册。 |
| `POST /api/v1/connectors/heartbeat` | Connector 上报 online/degraded runtime 状态。 |
| `GET /api/approval-requests` | 内部 approval list。 |
| `POST /api/approval-requests` | 创建内部 approval request。 |
| `POST /api/approval-requests/{id}/approve` | 内部 approve。 |
| `POST /api/approval-requests/{id}/reject` | 内部 reject，必须带 reason。 |
| `POST /notifications/send` | 发送或登记 Gateway-owned notification。 |

## Console

当前 Console 前端源码位于 monorepo 的 `apps/aiops_console_web`。

Console 独立构建和部署；Gateway 保留 `/api/*`、`/auth/*`、RBAC、审计、通知和待 V1 replacement acceptance 后删除的可选 `AIOPS_CONSOLE_DIST_DIR` legacy path。浏览器不得直连 diagnosis service、Connector、MCP、Prometheus、Loki 或 Feishu。

Platform Administrator 在 `/admin` 创建一对一 Connector Enrollment。credential 只在创建或轮换响应中显示一次；将其写入受管 Cluster 的 `AIOPS_CONNECTOR_CREDENTIAL` Secret。Cluster 只在 Connector 首次认证注册后出现，mutation 默认关闭。

## Approval 规则

- Approval state 在 Gateway/control-plane。
- Feishu notification card 可以链接到 Console，但不能 approve/reject。
- Reject 必须带 reason。
- Terminal state 只读。
- Gateway 的 RBAC/scope/status/expiry 校验是权威。

## Evidence 规则

- 缺失的 Prometheus/Loki/Topology/K8s evidence 必须显示为 unavailable、failed、empty、skipped 或 partial。
- 不得伪造 evidence ref。
- 缺失 source 明确时，partial diagnosis 是有效结果。
- Root-cause confidence 必须反映 evidence quality。
