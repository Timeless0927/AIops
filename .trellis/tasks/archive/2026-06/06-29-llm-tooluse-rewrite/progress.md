# Progress — child 2 (06-29-llm-tooluse-rewrite)

> 续接备忘。新窗口读此 + prd/design/implement.md 即可续。

## 已完成（已 commit）

- `cfb2f46` — child 1 provider 层（`hermes/diagnosis_provider.py` + test）,status=completed。
- `9251de3` — child 2 存储半:`toolsets/incident_store.py` 新 `diagnosis_trace` 表 + `add_diagnosis_trace`/`list_diagnosis_trace`(类法 + 模块级 wrapper) + `test_incident_store.py::test_diagnosis_trace_round_trip`(21/21 绿)。`toolsets/cost_guard.py` 加 `latency_ms` 列 + `_ensure_cost_columns` idempotent ALTER + `record_cost` `latency_ms` kw。

## child 2 收尾 commit（本次）

Step 3-6 全部落:
- `toolsets/incident_diagnosis.py` — LLM tool-use 重写:`COLLECTOR_VERSION=llm-tooluse-v1` + `FALLBACK_COLLECTOR_VERSION=keyword-v1`;新 helpers(`_TooluseAccumulator`/`_LLM_TOOL_SCHEMA`/`_build_tool_args_from_llm`/`_build_tooluse_system_prompt`/`_runbook_hints_for_alert`/`_record_observation_step`/`_run_llm_tooluse_session`/`_add_trace_row`/`_record_provider_cost`/`_diagnosis_from_llm`/`_apply_confidence_guardrail`/`_compose_diagnosis`);`run_diagnosis_session` 加 `provider` 参 — LLM 优先,异常→关键词 fallback,共用 `_collect_evidence`/`_derive_session_status`/`_persist_diagnosis`。
- `hermes/service_main.py` — provider 注入(`_resolve_diagnosis_provider` 进程级单例 + sentinel + `import logging`/`logger`);删 3 个 `_synthetic_*_adapter` + 无引用 `_evidence_ref`;3 个真 adapter(metrics/logs/k8s_read)的"MCP URL 未配置"分支改 `_unconfigured_partial`(partial gap envelope,不造假证据,像 `_topology_adapter`);`start_diagnosis_session` 调 `run_diagnosis_session` 加 `provider=_resolve_diagnosis_provider()`。
- `tests/test_diagnosis_llm_tooluse.py` — 新,5/5 绿。
- `tests/test_hermes_diagnosis_service.py` — 2 个端到端集成测断言对齐删 synthetic 后真实行为(无 MCP URL 时四路 partial gap → status `needs_human`、evidence_chain/timeline evidence_refs 空、missing_evidence topology 用 any),旧断言依赖已删 synthetic 造假证据。

验证:全绿 77 passed(test_incident_diagnosis/test_incident_evidence_collection/test_diagnosis_provider/test_incident_store/test_diagnosis_llm_tooluse/test_hermes_diagnosis_service/test_cost_guard);grep `_synthetic` 0 命中、`COLLECTOR_VERSION` 已迁 `llm-tooluse-v1`。

## 关键避坑

- `_collect_evidence`/`_persist_diagnosis` 的 `except ValueError: if incident_store is not None: raise` 守卫 — **传原始 incident_store,不可预解析**(预解析会把 None 变 module → 守卫误重抛测试 fake incident_id 的 ValueError)。child 2 已踩过。
- `_derive_session_status` 语义:`evidence_refs` 空 → `needs_human`(跑了工具但零成功证据即 needs_human)。**不可**改成「有 missing_evidence/partial gap 就 partial」——会让 `test_incident_diagnosis.py::test_diagnosis_session_*_needs_human`/`all_backends_unavailable_needs_human` 回归(acceptance line 46 硬约束)。删 synthetic 后 hermes 端到端从 partial 滑到 needs_human 是预期(测试断言已对齐,见 design line 78:删除造假的代价即此)。`partial` 专留给「有 ≥1 成功证据但不全」。
- provider `chat_with_tools` 单步推进;循环 glue 在 `_run_llm_tooluse_session`;tool schema 字段可选,`_build_tool_args_from_llm` 用 `_build_tool_args` 补默认,LLM 传值 override。
- 出境日志:`load_from_env()` + `_resolve_diagnosis_provider` 的 `logger.warning` 一行,复用 Issue A stdout→alloy→Loki 采集面,不双写 audit_log。

## child 2 完成后

- child 2 → `task.py` 加 commit + completed。
- 进 child 3 `06-29-replay-eval-harness`(PRD 已写;需补 design+implement 后 start)。
- parent `06-29-adr0003-diagnosis-brain` 跨 child 端到端 smoke(fake provider 全链路:四路 evidence + trace ≥5 + latency>0 + 降级 needs_human/partial + 出境日志可见 + 全仓 pytest 绿)。
- submodule 解耦另起独立任务(不进本 parent;record_cost/test_cost_guard 受 `tools` ImportError 阻塞记 submodule-decouple 待办)。