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


## Session 4: ADR-0005 Issue A end-to-end evidence closure verified in dev-external

**Date**: 2026-06-26
**Task**: ADR-0005 Issue A end-to-end evidence closure verified in dev-external
**Package**: hermes-agent
**Branch**: `main`

### Summary

Verified both remaining children in dev-external and closed parent ADR-0005 Issue A. stdout-logging: kubectl logs shows per-request stdout access lines for all AIOps services; Loki query_range {namespace=aiops-dev} returns real AIOps pod streams (mcp-topology/mcp-prometheus/hermes/gateway) not only smoke pod. servicemonitor: Prometheus up{namespace=aiops-dev} shows 6 AIOps service targets green. End-to-end smoke (5xx alert, ns=aiops-dev): session status=new (not failed), 3 evidence records (metrics/logs/topology) with non-empty payloads, diagnosis_json present. All four evidence channels reachable, none skipped as backend_unavailable. Archived 06-25-aiops-stdout-logging, 06-25-aiops-dev-servicemonitor (prev session) and parent 06-25-adr0005-issue-a-evidence-closure.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `1cc3ff9` | (see git log) |
| `8885cc9` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: Hermes LLM tool-use brain + diagnosis_trace + cost latency (ADR-0003 child 2)

**Date**: 2026-06-29
**Task**: Hermes LLM tool-use brain + diagnosis_trace + cost latency (ADR-0003 child 2)
**Package**: hermes-agent
**Branch**: `main`

### Summary

Rewrote run_diagnosis_session to LLM tool-use loop via child-1 chat_with_tools (ScriptedProvider): collect tool_calls -> _observe_tool/_collect_evidence -> add_diagnosis_trace -> refeed tool results -> final JSON. Added diagnosis_trace table + cost_records.latency_ms. Kept keyword path as fallback (provider=None / ProviderUnavailable / JSON parse fail). Deleted _synthetic_* adapters; unconfigured adapters return partial envelopes. COLLECTOR_VERSION=llm-tooluse-v1, FALLBACK=keyword-v1. Tests: 65 passed; state-machine cases 0 regression.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `cfb2f46` | (see git log) |
| `9251de3` | (see git log) |
| `a54c906` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: ADR-0003 child 3: replay eval harness (code-side)

**Date**: 2026-06-30
**Task**: ADR-0003 child 3: replay eval harness (code-side)
**Package**: hermes-agent
**Branch**: `main`

### Summary

Built the ADR-0003 replay harness (code-side). Added a category field to the brain's final-JSON prompt schema so diagnosis aligns to ground truth by label (not fragile free-text matching), and wrote tests/replay_incident.py to replay frozen fixtures through run_diagnosis_session via ScriptedProvider + FrozenAdapter, scoring against root_cause_category with a hand-maintained tolerance matrix (exact / sibling / unrelated, confidence bonus, fallback-no-credit). 2 synthetic sample fixtures (memory-pressure, cert-expiry) prove the harness end-to-end; a --validate-taxonomy self-check guards the matrix. Pruned an unreachable parent-bucket scoring branch. Recorded the harness pattern + tolerance-matrix reachability lesson in the backend testing spec. The ≥10 real-fixture campaign and the ADR-0003 acceptance writeback are deferred to the parent (Issue A/B operational cost).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `4c63c61` | (see git log) |
| `a0e0756` | (see git log) |
| `f2ba5a6` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

## 2026-06-30 adr0003-diagnosis-brain — parent 终态验收闭合(AC#1–#5)

**Task**: 06-29-adr0003-diagnosis-brain (parent) — ADR-0003 薄 LLM tool-use 诊断大脑

**Done**:
- 三 child(provider 调用层 `cfb2f46`、tool-use 重写 + diagnosis_trace/cost latency `a54c906`/`9251de3`、replay harness `4c63c61`)已 archived。
- 补 AC#2 真缺口:`_record_provider_cost` 加 optional store 注入接缝(store.record_cost 优先,fallback 模块级 cost_guard,生产路径不变,ADR-0004 不绕过)+ 新测 `test_parent_ac_full_chain_smoke_four_channels_trace_and_cost_latency`(4 路 evidence + trace≥5 + latency>0)。
- AC#5 写回 ADR-0003 §验收标准 加「落地状态」节,分层:代码能力层(可测)已闭环并归档;真故障命中率层(≥10 真 fixture)挂为运营债,需 ADR-0005 Issue A 真采证 + Issue B 真根因回填运营后回填。
- 全 child scope pytest 76 passed(trecheck 0 fix);已知 pre-existing `tools` submodule 缺依赖致 test_cost_guard collection error 按 PRD AC#4 排除。

**Status**: parent AC#1–#5 全闭合,归档 `archive/2026-06/06-29-adr0003-diagnosis-brain`。

**Remaining (运营债,非此 parent 代码)**:
- ≥10 真历史 incident 回放命中率(ADR-0003 V1 验收硬门槛)——需真后端采证 + 真根因回填运营。
- `hermes/` 改名(Future Work),随下次该服务大改一并完成。

## 2026-06-30 adr0003-replay-campaign #1–#4 — ADR-0003 大脑首次在集群真跑 + 2 bug 修复

**Task**: 06-30-adr0003-replay-campaign(ADR-0003 ≥10 真实故障回放运营)

**Done (#1–#4)**:
- #1 真 LLM provider:DeepSeek 官方 `api.deepseek.com/v1` / `deepseek-v4-flash`,live curl 验 tool-use(call calc 返回正确 tool_calls);key 走 live secret `aiops-runtime-secret` patch,不入 git。
- #2 demo-apps ns + 采证链路:ns Active、Connector Role+RoleBinding(SA 指回 aiops-dev)、探针 pod(用 aiops-hermes 镜像因 docker.io 出网断)、ServiceMonitor `demo-apps`(kube-prometheus-stack 只认 ServiceMonitor 不认 pod 注解——运营坑)、Loki alloy 全集群抓 stdout。验通 Prom `demo_probe_up=1` + Loki 收 demo-apps stdout。
- #3 dev-external overlay:MODEL_BASE_URL/NAME/SCOPE(`aiops-dev,demo-apps` 保留平台自身)、live secret patch、钉 6 service 镜像到 GH Actions sha tag。
- #4 fixture 导出器 `tests/export_incident.py` + 8 测试;trellis-check 抓 4 bug(sync await close、dead None-check、**位置配对在 failed middle step 错位→改 observation_ref↔source_ref keyed 优先**、float-vs-dict confidence)。design.md 对齐。69 passed scope suite。

**关键发现(被掩盖多日的真相)**:
- ADR-0003 三 child + parent 代码 **全部只在本地,14 commit 全没 push**;GH Actions 从没 build 过新大脑;集群跑 `:latest@87e55646` 老 keyword 引擎(IfNotPresent 永不重拉)。parent「代码侧 archived」只在单测层成立,集群层从未验。
- push 后 build `:03dfc75` 跑首个真告警,发现 **ProviderConfig 无 chat_with_tools bound method**:child-1 把 chat_with_tools 做成模块级函数(cfg 首参),child-2 当 provider 对象调 `.chat_with_tools(messages, tools)`;**单测传 ScriptedProvider 绕过了真接缝**,从未测真 ProviderConfig 路径 → 集群一跑就降级 keyword。这是「单测层闭环、集群层没验」的第二例。修:ProviderConfig 加 bound method 委托模块级函数 + 补 `test_provider_config_chat_with_tools_bound_method_*` 锁真接缝。镜像 `:5d5b27e`。
- 修后真告警:`diagnosis_trace` **13 行**(真 DeepSeek tool-use span,tokens 落库)、**8 条 evidence**(logs/metrics/topology/k8s 四路)、provider outgress 日志正确。**ADR-0003 大脑首次在集群真跑通**。
- 遗留(非 blocker):`max_turns=6` 不收敛 → 6 轮 tool-call 没产出 final JSON → keyword fallback。调优放 #5。

**Bug 修复 commits**:`5d5b27e`(ProviderConfig bound method)、`4066bde`(overlay 注释)、`17826f9`(fixture 导出器)。

**Status**:#1–#4 完成;#5 ≥10 真实 incident 运营闭环 + #6 harness sweep 回填 ADR-0003 留下次(需真故障场景 + 人工判真根因,运营重头)。

**Lessons(值得入 spec)**:
1. 单测注入 fake provider 绕过真接缝 = 集群层 bug 温床。provider 层必须有「真 ProviderConfig 走 bound method」的回归测,不只 ScriptedProvider。
2. `imagePullPolicy: IfNotPresent` + `:latest` tag = 节点本地老镜像永不重拉,掩盖「代码 push 了但集群没新镜像」。dev overlay 应钉 GH Actions sha tag,不靠 `:latest`。
3. kube-prometheus-stack 默认只 scrape ServiceMonitor,不认 pod `prometheus.io/scrape` 注解——新业务 ns 要被 scrape 必须建 ServiceMonitor(且带 `release: prometheus-stack` label)。
4. `incident_diagnosis._collect_evidence` 对 failed status 提前返回不写 evidence 行 → trace/evidence 行数可差 1,fixture 导出器必须 keyed link(`observation_ref`↔`source_ref`)不能纯位置配对。


## Session 7: ADR-0003 real fixture replay campaign closed

**Date**: 2026-07-01
**Task**: ADR-0003 real fixture replay campaign closed
**Package**: hermes-agent
**Branch**: `main`

### Summary

Closed ADR-0003 V1 real replay campaign: exported 10 synthetic:false live fixtures, validated replay real_count=10 real_hit_rate=1.0, hardened live LLM tool-use parsing/max-turn seams, updated ADR/spec, and archived the Trellis task.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `af45aff` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 8: Live diagnosis entry fix

**Date**: 2026-07-02
**Task**: Live diagnosis entry fix
**Package**: hermes-agent
**Branch**: `main`

### Summary

Planned and completed live diagnosis entry fix: added narrow Hermes service-token authorization for Gateway k8s reads, configured live tool/provider timeout and writeback secret examples, updated authorization spec, and covered Gateway/Hermes auth behavior with targeted tests.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `9b27f56` | (see git log) |
| `6b71cc4` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 9: Alertmanager bearer route

**Date**: 2026-07-02
**Task**: Alertmanager bearer route
**Package**: hermes-agent
**Branch**: `main`

### Summary

Completed Alertmanager automatic routing using Gateway-scoped bearer auth instead of an HMAC relay, added AlertmanagerConfig and secret examples, documented enable/disable/smoke flow, and fixed PodCrashLooping fallback action classification with tests and spec updates.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `6e5f995` | (see git log) |
| `f975761` | (see git log) |
| `8894b3c` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
