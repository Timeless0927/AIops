# CLAUDE.md

## 项目概述

AIOps 是面向 Kubernetes 告警诊断和受控运维的 source monorepo；控制面保持 split-service，Console 保持独立发布制品。

当前架构入口：`docs/README.md` 和 `docs/current-architecture.md`。

## Agent skills

### Issue tracker

Issues and PRDs are tracked as local Markdown under `.scratch/` in this monorepo; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

The default five-role vocabulary is used: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This monorepo uses one root `CONTEXT.md` and system-wide `docs/adr/`. See `docs/agents/domain.md`.

## 当前核心边界

- `apps/aiops_k8s_gateway`：Gateway/control-plane，负责 Alertmanager ingress、incident/session、RBAC、内部审批、通知、审计、Connector routing 和 diagnosis writeback。
- `diagnosis_service/`：diagnosis service，负责诊断编排、证据组织、结构化诊断输出和 writeback。
- `apps/cluster_connector`：集群内 Connector，执行 Gateway 授权的 Kubernetes command envelope。默认部署为 read-only。
- `apps/mcp_prometheus`、`apps/mcp_loki`、`apps/mcp_topology`：Prometheus/Loki/Topology MCP evidence 服务。
- `apps/aiops_console_web`：Console Web source workspace，拥有独立 package lock、构建和 `aiops-console` 发布制品；Gateway 仍只拥有 API、认证和 event stream。
- `aiops/contracts`、`aiops/domain`、`aiops/k8s`：共享协议、领域模型和 Kubernetes envelope。
- `runtime/`：镜像 smoke 和后台 worker；`toolsets/`：Gateway/diagnosis/MCP 仍使用的本地工具实现。

## 当前产品决策

- Diagnosis service 是短期诊断大脑，不做 Brain Provider 抽象。
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
python -m diagnosis_service.service_main --help
python -m apps.aiops_k8s_gateway.main --help
cd apps/aiops_console_web && npm ci && npm run build
kubectl apply -k deploy/k8s/overlays/dev-bundled
kubectl apply -k deploy/k8s/overlays/rc-bundled-digest
```

## 文档与状态

- `docs/README.md` 是当前文档入口。
- `docs/current-architecture.md` 和 `docs/architecture-diagrams.md` 是最新架构留档。
- `.scratch/aiops-v1-model/PRD.md` 是 V1 产品与 Console/Gateway handoff 规格。
- `deploy/k8s/README.md` 是部署和 smoke 命令事实源。
- 旧产品/前端/实验性代理文档不再作为当前事实源；需要历史证据时查 Git 历史。

## 开发约定

- 新代码优先落在 `apps/` 和 `aiops/` 的明确边界内。
- Gateway 与 Connector 通过 contracts/envelopes 通信，不直接导入对方内部实现。
- 浏览器不得直连 Diagnosis、Connector、MCP、Prometheus、Loki 或 Feishu approval API。
- 高风险或会改变集群状态的能力必须经过 Gateway-owned approval、RBAC、audit、dry-run/lock/post-check/rollback 等后续安全链路。
- 面向人阅读的项目文档以中文为主；代码标识符、路径、命令、API 字段和错误码保留英文。
