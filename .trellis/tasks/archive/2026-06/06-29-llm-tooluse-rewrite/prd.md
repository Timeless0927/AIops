# Thin LLM tool-use rewrite + diagnosis_trace + cost latency

Parent: `06-29-adr0003-diagnosis-brain`。依赖 child 1(`06-29-diagnosis-provider-layer`)。

## Goal

把 `toolsets/incident_diagnosis.py` 的关键词步骤循环(`run_diagnosis_session:102-125`)替换为 LLM tool-use 循环:
四个 MCP adapter 注册为 LLM tools,模型现场读证据自主决定调用顺序,推理根因,输出结构化 diagnosis。
同步新增 `diagnosis_trace` 表(ADR-0005 Issue E)+ `cost_records.latency_ms` 列(共切入点埋点)。

## Requirements

### 替换(规则引擎 → LLM)
- `run_diagnosis_session:102-125` 步骤循环 → tool-use loop:4 个 MCP adapter 包成 LLM tools
  (JSON schema + 动态 `_build_tool_args`),调 child 1 `chat_with_tools`,tool_call → `_observe_tool`+`_collect_evidence`+`_redact_payload`,
  observation 回灌为 tool message,至无 tool_call 收结构化 diagnosis 对齐 `build_diagnosis` schema(:60-72)。
- 删 `_build_session_plan:193`、`_build_action_proposals:621`、`_build_root_cause_candidates:779`(改由 LLM 输出)。
- 删 `hermes/service_main.py:585-631` 的 4 个 `_synthetic_*_adapter`(tool-use 路径下无意义);保留 4 个真 MCP adapter + 注入面,
  顺带把 provider cfg 一并传入。
- `COLLECTOR_VERSION:15` `keyword-v1` → `llm-tooluse-v1`。

### 保留(钉死不动,复用)
- `_collect_evidence:655`+`_redact_payload:695`+`_evidence_window:713`、`_observation_from_envelope:450`+`_as_mapping:516`+`_first_evidence_ref:533`、
  `_derive_session_status:591` 状态机、`build_diagnosis:34`+`render_markdown:153`+`to_json:188`、`_build_tool_args:226`(k8s selector:315-380)、
  writeback 全链路、`add_evidence:477`+`record_incident_diagnosis:620`+`find_similar_case_profiles:864`。

### 置信度护栏(embedded decision)
- `_score_confidence:817`+`_confidence_for_observation:574`+`_confidence_level:827` **保留**为下限护栏:
  LLM 输出 score 取 `max(llm_score, _score_confidence(...))` 并标 `degraded`。信任边界内数值校验,不可省。

### 新增存储
- `toolsets/incident_store.py`:加 `diagnosis_trace` 表(ADR-0005 §决策 2 钉死字段:
  session_id/step_index/tool_name/tool_args_json/observation_ref/duration_ms/model/input_tokens/output_tokens/trace_collected_at)+
  新增 `_ensure_diagnosis_trace_*`(套 `_ensure_*_columns:258` 范式)+`async add_diagnosis_trace(...)`,每个 LLM step 落一行。
- `toolsets/cost_guard.py:30` `cost_records` 加 `latency_ms INTEGER` 列;`__init__:115` 后补 idempotent ALTER;`record_cost:183` 加 `latency_ms` 入参。

### Prompt 引线
- runbook 按 alert-type 检索 `skills/sre/runbooks/{...}/` 拼进 system prompt(空库也跑通先空)。
- 案例记忆:`_derive_incident_signature(incident)` → `find_similar_case_profiles(signature, exclude_incident_id=...)`,
  命中才拼 `optional_memory_hints`(不可否决现场证据,泛化后置)。
- trace 脱敏:`tool_args_json` 走 `_redact_payload`,prompt 快照走 `_redact_sensitive_text` 入库前兜底;出境关卡随 child 1 provider 层(本 child 不深做)。

## Constraints

- `build_diagnosis` 输出 schema(markdown/confidence.score/level)不变 —— writeback 口径依赖。
- 现有状态机测试(`needs_human`/`partial`/`diagnosed`)不回归。
- 执行边界:模型产出的 mutation 仍只是 action proposal,经 Gateway,Hermes 内不执行。

## Acceptance Criteria

- [ ] `pytest tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py tests/test_incident_store.py -q` 全绿,
      状态机用例 0 回归(测试改面:注入 child 1 `ScriptedProvider`,关键词路径断言→LLM 输出 diagnosis 断言,约 12-15 用例)。
- [ ] 脚本 tool-use 跑通一遍:四路 evidence 落库(`add_evidence` 被调)、`diagnosis_trace` ≥1 行、`cost_records.latency_ms` 有值。
- [ ] provider 不可达 → `_derive_session_status` 走 needs_human(无证据)/partial(有证据),不崩溃。
- [ ] 出境日志(child 1 提供)在测试输出可见;`COLLECTOR_VERSION` 迁 `llm-tooluse-v1`。

## Verification

`pytest tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py tests/test_incident_store.py tests/test_diagnosis_provider.py -q`。
rollback:`incident_diagnosis`/`incident_store`/`cost_guard` 三文件回滚,child 1 provider 模块独立保留。