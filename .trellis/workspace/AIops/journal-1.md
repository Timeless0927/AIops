# Journal - AIops (Part 1)

> AI development session journal
> Started: 2026-06-24

---

## 2026-06-25 deploy-monitor-switch-aiops-external — 部署成,Issue A 锚点未达

**Done**:
- `loki` ns 起监控栈 (kube-prometheus-stack + loki + alloy agg/agent),k8snode-1 打 `ops=test`,harbor imagePullSecret 建好(不入库)。
- 改 `deploy/k8s/overlays/dev-external/kustomization.yaml` 三值: `PROMETHEUS_URL`→`prometheus-stack-kube-prom-prometheus.loki.svc:9090`、`LOKI_URL`→`loki.loki.svc:3100`、`AIOPS_NAMESPACE_SCOPE`→`aiops-dev`。
- AIOps 切 dev-bundled → dev-external: 6 pod (gateway/connector/hermes/mcp 三件) Running,bundled-only(`aiops-dev-prometheus`/`aiops-dev-loki`/`payment-api`)已删,healthz smoke ok。
- 后端可达验证: Prometheus `/api/v1/query` success (22 条 up)、Loki `/ready` ready、`namespace` label 含 `aiops-dev`。

**Not done (ADR-0005 Issue A 端到端锚点)**:
告警 → Gateway → Hermes 跑出 incident `30cc41b1-...`,但 session `status failed`,`incident_analysis` 未写,只落 1 路 logs evidence 且 `line_count:0`。三路 metrics/k8s_read/topology 无 evidence row。

**Root cause = 三个 spec 缺口,均非部署/切换侧,PRD Out of Scope + 零代码改动**:
1. **AIOps 应用不发 stdout**: `kubectl logs` 对 6 个 AIOps pod 全空,Loki `namespace=aiops-dev` 只有冒烟 pod `aiops-alertmanager-smoke`。alloy 抓不到不存在的东西 → logs `line_count:0`。违反 backends/logging-guidelines。
2. **无 aiops-dev 的 ServiceMonitor**: Prometheus `up` 22 条全是 k8s 系统组件,无 aiops-related。kube-prometheus-stack 默认不 scrape 业务 ns → metrics 路 skipped。
3. **诊断对空后端短路失败**: `run_diagnosis_session` 在无 target 告警 + 空后端下 derive 出 `failed`,`incident_analysis` 不写。`incident_diagnosis.py:591 _derive_session_status` + `TERMINAL_FAILURE_CODES`。

**Decision (user)**: 收尾 = A。部署/切换本身算成;三个缺口记进 spec 作后续独立任务锚点,提交 overlay 改动。

**Spec updates made**:
- 新建 `.trellis/spec/deploy/` (index + `dev-external-observability-contract.md`) — 把 overlay→env→MCP→evidence 的跨层契约、四路 evidence 验收"]["正确/错误"、三个 prerequisite 缺口全落成 executable contract。
- `backends/logging-guidelines.md` 加「AIOps services must emit lifecycle lines to stdout (Loki collection surface)」段,点明 stdout 是 alloy 采集面,与 audit_log durable channel 并存不替代。
- `guides/cross-layer-thinking-guide.md` 加「Deploy Overlay → Runtime Env → Evidence Collection Boundary」checklist,带本次真实反例(overlay 对、smoke 仍 failed)。

**Pitfalls hit during debugging**:
- `kubectl exec` 不加 `-i` 时 heredoc stdin 被吞,`python3 -` 读空直接静默退出(无输出无报错)。必须 `kubectl exec -i`。
- 单行长 `python3 -c` 在终端折行时被插真实换进字符串导致 SyntaxError — 改 heredoc + `python3 -` 避开。
- `incident_events` 表无 `created_at` 列(只有 `id` 自增)。timeline 查询用 `ORDER BY id`。
- "evidence 行数 > 0" 不等于成功: `_collect_evidence` 对 skipped/partial 也写行,且 session failed 时失败步前的行仍在。pass 信号要看 `investigate_end` 非 failed + `incident_analysis` 已写 + payload 非空。

**Next (后续任务)**: 
- AIOps app stdout 日志接入。
- AIOps 自身 ServiceMonitor。
- `incident_diagnosis` 对空后端容错(status=partial/needs_human 且仍写 analysis)。

## Session 1: Deploy monitor stack + switch AIOps to dev-external

**Date**: 2026-06-25
**Task**: Deploy monitor stack + switch AIOps to dev-external
**Package**: hermes-agent
**Branch**: `main`

### Summary

loki ns 部署 kube-prometheus-stack+loki+alloy,k8snode-1 打 ops=test;dev-external overlay 三值改对(PROMETHEUS_URL/LOKI_URL/AIOPS_NAMESPACE_SCOPE=aiops-dev)并提交(984d66b);AIOps 从 dev-bundled 切到 dev-external,6 核心 pod Running,bundled 假后端已删,healthz/后端可达性验证通过。部署/切换本体达成。ADR-0005 Issue A 端到端锚点未达:session failed、incident_analysis 未写、四路 evidence 仅 logs 且 line_count:0,根因三个 spec 缺口(应用不发 stdout、无 aiops-dev ServiceMonitor、诊断对空后端短路 failed),均属 PRD Out of Scope+零代码改动,已沉淀进 .trellis/spec/deploy/ observability contract、backends/logging-guidelines.md、guides/cross-layer-thinking-guide.md,作后续独立任务锚点。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `984d66b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
