# CLAUDE.md

## 项目概述

AIOps 是面向 Kubernetes 告警诊断和受控运维的 split-service control plane。当前主线不是旧的 all-in-one Feishu SRE Agent，也不是 hermes-agent 业务 fork。

当前架构入口：`docs/README.md` 和 `docs/current-architecture.md`。

## 当前核心边界

- `apps/aiops_k8s_gateway`：Gateway/control-plane，负责 Alertmanager ingress、incident/session、RBAC、内部审批、通知、审计、Connector routing 和 diagnosis writeback。
- `hermes/`：Hermes diagnosis service，负责诊断编排、证据组织、结构化诊断输出和 writeback。命名说明：`hermes/`（及 `AIOPS_HERMES_*`、`"service": "hermes"`）指自研诊断服务边界，与已删除的 NousResearch `hermes-agent` 外部项目无关，重名是历史遗留，改名见 ADR-0003 future work。
- `apps/cluster_connector`：集群内 Connector，执行 Gateway 授权的 Kubernetes command envelope。默认部署为 read-only。
- `apps/mcp_prometheus`、`apps/mcp_loki`、`apps/mcp_topology`：Prometheus/Loki/Topology MCP evidence 服务。
- `apps/aiops_console`：Console V1 静态 vertical slice，生产前端只通过 Gateway `/api/*`。
- `aiops/contracts`、`aiops/domain`、`aiops/k8s`：共享协议、领域模型和 Kubernetes envelope。
- `hooks/`、`runtime/`、`toolsets/`：V1 迁移期 legacy compatibility layer，新领域逻辑默认不继续沉到这里。

## 当前产品决策

- Hermes 是短期诊断大脑，不做 Brain Provider 抽象。
- Gateway/control-plane 是入口、权限、审批、通知、审计、执行授权的唯一权威边界。
- Feishu 是 notification-only channel，只能通知和跳转内部 Console，不能改变 approval 状态。
- Approval Center 走内部 Gateway Approval Service API。
- P0/P1 默认只读诊断，不执行 Kubernetes mutation。
- 当前部署路径是 native Kubernetes YAML，不做 Helm。
- Grafana 只做既有 dashboard/panel embed 兼容，不由 AIOps 部署或重写。

## 常用命令

```bash
pip install -r requirements.txt
pytest tests/
python -m hermes.service_main --help
python -m apps.aiops_k8s_gateway.main --help
kubectl apply -k deploy/k8s/overlays/dev-bundled
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

## 文档与状态

- Multica issue 是任务状态、验收结论、阻塞、PR、commit 和剩余风险的事实源。
- `docs/README.md` 是当前文档入口。
- `docs/current-architecture.md` 和 `docs/architecture-diagrams.md` 是最新架构留档。
- `docs/aiops-console-v1-contract.md` 是 Console V1 Gateway API handoff。
- `deploy/k8s/README.md` 是部署和 smoke 命令事实源。
- 已删除的旧 `00-PDD` 至 `05-TDD`、`CHANGE-REQUESTS`、`TODD`、`development-progress`、`hermes-sre-agent-*`、`feishu-sre-agent-*` 和 `docs/superpowers/*` 不再作为当前事实源。

## 开发约定

- 新代码优先落在 `apps/` 和 `aiops/` 的明确边界内。
- Gateway 与 Connector 通过 contracts/envelopes 通信，不直接导入对方内部实现。
- 浏览器不得直连 Hermes、Connector、MCP、Prometheus、Loki 或 Feishu approval API。
- 高风险或会改变集群状态的能力必须经过 Gateway-owned approval、RBAC、audit、dry-run/lock/post-check/rollback 等后续安全链路。
- 面向人阅读的项目文档以中文为主；代码标识符、路径、命令、API 字段和错误码保留英文。
