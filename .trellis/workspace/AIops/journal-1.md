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


## Session 2: A-3: diagnosis tolerates unreachable backends

**Date**: 2026-06-25
**Task**: A-3: diagnosis tolerates unreachable backends
**Package**: hermes-agent
**Branch**: `main`

### Summary

ADR-0005 Issue A 闭环 parent(06-25-adr0005-issue-a-evidence-closure)+ 三 child 建立。完成 child A-3(diagnosis-empty-backend-tolerance):改 _derive_session_status,单路后端不可达(backend_unavailable/connector_offline/timeout)不再一票否决整 session=failed。新语义:无 evidence→needs_human;有 evidence 且 hard_failure/missing/partial→partial;全成功→diagnosed;failed 仅留非法状态兜底。改名 test_..._fails_controlled→_degrades_not_fails(断言改 needs_human),新增 one-backend-down→partial、all-down→needs_human 两用例。研究双向证实触发链路是 Gateway webhook→Hermes service→run_diagnosis_session,校正 journal 两处误前提(diagnosis_json 无条件写、incident_analysis 表不被 Hermes 路径写)。spec 新增 hermes-agent/backend/diagnosis-session-status.md(7段契约+跨层消费方核查)。测试 44+21 绿,全量失败均为环境缺依赖无关。commit 345e90e。兄弟 child A-1(aiops-stdout-logging)、A-2(aiops-dev-servicemonitor)已建未规划。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `345e90e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

## Session 3: A-1: AIOps services emit stdout lifecycle logs (planning + impl)

**Date**: 2026-06-26
**Task**: 06-25-aiops-stdout-logging (ADR-0005 Issue A child, lightweight PRD-only)
**Package**: hermes-agent / apps
**Branch**: `main`

### Summary

把 parent ADR-0005 Issue A 下两个未规划 child 推进到可执行,并完成 child A-1(stdout)。A-1 根因读码定位到单点:`apps/service_http.py:38` `JsonHandler.log_message` 静默 `return` 吞掉 `BaseHTTPRequestHandler` 自带 access-log,6 服务全空 stdout。修法 = 覆盖 `log_request` 向 stdout 写一行访问日志(address/requestline/code/size),`log_message` 保持静默避免 incidental `log_error` 噪音。6 服务全继承 JsonHandler 故一处覆盖全部,无新依赖、无逐服务改动。自检 `__main__`(起 ThreadingHTTPServer + /healthz + 断言 stdout 非空含 200)绿;`test_observability_mcp_runtime.py` 5 passed 不回归;全量 88 failed 全是 pre-existing env 缺陷(tools/aiohttp 未装、hermes-agent 子模块未检出、sqlite schema 漂移),无一条命 service_http。spec 在 logging-guidelines.md §stdout 补「Where it is implemented」段指明落点与 stale-image 排障。待办:部署到 dev-external 实地验 kubectl logs 非空 + Loki 可查(child AC 的集群项);兄弟 child A-1(stdout)代码侧收口,A-2(aiops-dev-servicemonitor)PRD+design+implement 已就绪待激活。

### Main Changes

- `apps/service_http.py`:`JsonHandler.log_request` 写 stdout 访问日志一行/请求(`sys.stdout.write`+flush),`log_message` 维持静默;加 `__main__` 自检。
- `.trellis/tasks/06-25-aiops-stdout-logging/prd.md`:填实 PRD(根因、Requirements、AC)。
- `.trellis/tasks/06-25-aiops-dev-servicemonitor/{prd.md,design.md,implement.md}`:规划 ServiceMonitor child(全做:/metrics + 端口 + SM;手写 stdlib exposition 不引 prometheus_client;共用 http 端口;SM selector 需集群实测为硬门)。
- `.trellis/spec/hermes-agent/backend/logging-guidelines.md`:§stdout 补「Where it is implemented」段。

### Git Commits

| Hash | Message |
|------|---------|
| `1cc3ff9` | feat(logging): AIOps services emit stdout access line per request (ADR-0005 Issue A child A-1) |

### Testing

- [OK] `python3 apps/service_http.py` → `ok: 127.0.0.1 "GET /healthz HTTP/1.1" 200 -`
- [OK] `pytest tests/test_observability_mcp_runtime.py` → 5 passed
- [OK] ast parse; no service_http references in any pre-existing env failure

### Status

[OK] **代码侧完成**;集群验收(kubectl logs 非空 + Loki 可查)待 dev-external 重部署。

### Next Steps

- dev-external 重部署后跑 A-1 集群 AC。
- 激活并执行 child A-2(aiops-dev-servicemonitor)。
- parent 端到端四路绿灯。


## Session 3: aiops-dev ServiceMonitor: /metrics exposition + scrape live

**Date**: 2026-06-26
**Task**: aiops-dev ServiceMonitor: /metrics exposition + scrape live
**Package**: hermes-agent
**Branch**: `main`

### Summary

Added hand-written /metrics exposition (no prometheus_client dep) to all 6 AIOps services via shared JsonHandler helper, plus a ServiceMonitor in base targeting aiops-dev (selector part-of=aiops-sre-agent, reuses existing http port). Deployed to dev-external: Prometheus sees all 6 services up. ADR-0005 Issue A metrics evidence channel now has data.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8885cc9` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
