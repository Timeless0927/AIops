# AIOps 用户手册

最后对齐日期：2026-06-17

## 当前系统能力

AIOps 接收 Alertmanager 告警，创建或复用 incident，触发 Hermes diagnosis，在可用时收集 Prometheus/Loki/K8s/Topology evidence，并通过 Gateway-controlled API 暴露结构化诊断 artifact。

当前边界：

- 支持 diagnosis。
- 默认 Kubernetes execution 是 read-only。
- Action proposal 可以要求 approval，但 P0/P1 不执行 mutation。
- Feishu 只负责通知和跳转链接。
- Approval 必须在内部 Approval Center / Gateway API 完成，不能在 Feishu 完成。

## 主要部署入口

当前使用 `deploy/k8s/` 下的 native Kubernetes YAML；本阶段不使用 Helm。

开发 bundled profile：

```bash
kubectl apply -k deploy/k8s/overlays/dev-bundled
```

外部 observability profile：

```bash
kubectl apply -k deploy/k8s/overlays/dev-external
```

禁用 observability profile：

```bash
kubectl apply -k deploy/k8s/overlays/dev-disabled
```

固定 digest 的 RC profile：

```bash
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

完整部署、镜像、Secret、profile 切换和 smoke 命令见 [deploy/k8s/README.md](../deploy/k8s/README.md)。

## 常用 Gateway Endpoint

| Endpoint | 用途 |
| --- | --- |
| `GET /healthz` | Gateway health。 |
| `GET /readyz` | Gateway readiness 和 connector count。 |
| `POST /webhooks/alertmanager` | Split Gateway Alertmanager ingress。 |
| `POST /diagnosis/writeback` | 受保护的 Hermes diagnosis artifact writeback。 |
| `GET /incidents/{incident_id}` | smoke/writeback tests 使用的受保护 lower-level incident view。 |
| `POST /auth/login` | Gateway auth/session 入口。 |
| `GET /auth/me` | 当前 actor 和 permission。 |
| `GET /api/approval-requests` | 内部 approval list。 |
| `POST /api/approval-requests` | 创建内部 approval request。 |
| `POST /api/approval-requests/{id}/approve` | 内部 approve。 |
| `POST /api/approval-requests/{id}/reject` | 内部 reject，必须带 reason。 |
| `POST /notifications/send` | 发送或登记 Gateway-owned notification。 |

## Console

当前可 review 的 Console slice 是静态页面：

```text
apps/aiops_console/static/incident-detail.html
```

它使用 fixture data，覆盖 complete、empty、partial 和 failed incident-detail 状态。生产 Console adapter 应遵循 [Console V1 契约](aiops-console-v1-contract.md)。

## Approval 规则

- Approval state 在 Gateway/control-plane。
- Feishu notification card 可以链接到 Console，但不能 approve/reject。
- Reject 必须带 reason。
- Terminal state 只读。
- 即使 frontend 显示或隐藏按钮，Gateway 的 RBAC/scope/status/expiry 校验仍是权威。

## Evidence 规则

- 缺失的 Prometheus/Loki/Topology/K8s evidence 必须显示为 unavailable、failed、empty、skipped 或 partial。
- 不得伪造 evidence ref。
- 缺失 source 明确时，partial diagnosis 是有效结果。
- Root-cause confidence 必须反映 evidence quality。

## 当前风险

- Topology evidence 仍可能受 runtime/data availability 影响而 partial。
- Gateway durable writeback 是生产 history 路径；只有 smoke export 不足以支撑长期 audit。
- Mutation execution 是后续阶段，必须另行补齐 approval、audit、dry-run、operation lock、post-check 和 rollback。
