# Design: Thin LLM tool-use rewrite + diagnosis_trace + cost latency

> Active task: `06-29-llm-tooluse-rewrite`(已 `task.py start`)。依赖 child 1(`hermes/diagnosis_provider.py`)。

## Goal Recap

把 `toolsets/incident_diagnosis.py:run_diagnosis_session` 的关键词步骤循环(:99-150)替换为
LLM tool-use 循环:四个 MCP adapter 包成 LLM tools,模型现场读证据自主决定调用顺序,推理根因,
输出结构化 diagnosis 对齐 `build_diagnosis` schema。同步新增 `diagnosis_trace` 表(ADR-0005 Issue E)
+ `cost_records.latency_ms` 列(共切入点埋点)。

## Key facts(读码确认)

- `build_diagnosis:34` 输出 schema(markdown/confidence.score/level/root_cause_candidates/evidence_chain/
  recommended_actions/...)是 writeback 口径,钉死不变。LLM 输出须对齐其入参而非另造 schema。
- `run_diagnosis_session:99-150` 流程:`_build_session_plan`(关键词)→ for step: `_observe_tool`+`_collect_evidence`
  → `_build_action_proposals`(关键词)→ `build_diagnosis` → `_derive_session_status` → `_persist_diagnosis`。
  要改的是 plan 选择(`_build_session_plan:193`)和根因/动作生成(`_build_action_proposals:621`/
  `_build_root_cause_candidates:779`),不动 evidence 采集(`_collect_evidence:655`/`_observe_tool`/
  `_observation_from_envelope`/`_as_mapping`/`_first_evidence_ref`)与状态机 `_derive_session_status:591`。
- 四个真 adapter 在 `hermes/service_main.py:366-426`,由 `start_diagnosis_session:136-141` 注入
  `run_diagnosis_session`。adapter 签名 `async (args: dict) -> ToolEnvelope`。
- `_build_tool_args:226`(k8s selector 解析:315-380)给每工具构造入参 dict —— LLM tool schema 要暴露
  这些 args 字段,让模型填。
- `incident_store._SCHEMA_SQL`(`:130 起`)+ `_ensure_incident_columns:258`/`_ensure_case_profile_columns:271`
  提供 idempotent `try ALTER / except sqlite3.OperationalError` 范式 —— diagnosis_trace 新表照加,
  cost_records.latency_ms 列照 ALTER 范式。
- `add_evidence:477` 签名含 `collector_version/confidence` —— 复用,_collect_evidence 已调。
- `cost_guard.py:30 _SCHEMA_SQL` cost_records 8 列无 latency_ms;`record_cost:183` 加 `latency_ms` 入参。
- runbook 已存:`skills/sre/runbooks/{certificate-expiry,high-memory,node-not-ready,pod-crashloop,pvc-full}/`,
  按 alert-type 目录;`find_similar_case_profiles`(incident_store:864)按精确 signature 取。

## Decision: tool-use 循环形状(单步推进,循环在本层)

child 1 `chat_with_tools` 是单步推进。本层 `run_llm_tooluse_session` 拥有循环:

```
messages = [system_prompt(runbook hints + similar cases), user(incident summary)]
tools = [4 个 MCP adapter 的 OpenAI tool schema]
loop (≤ max_turns):
    result = await chat_with_tools(provider, messages, tools)
    if result.tool_calls 为空 → 把 result.message 作为 final assistant diagnosis, break
    for call in result.tool_calls:
        args = call.arguments  (已 dict)
        observation = await _observe_tool(call.name, args, adapter_for(call.name))
        await _collect_evidence(incident, observation, args, store)   # 复用,落 evidence
        add_diagnosis_trace(store, session_id, step_index, call, observation, usage)  # 新埋点
        messages.append(result.message)              # assistant tool_call 消息
        messages.append({role:"tool", tool_call_id:call.id, content: json(observation summary)})
        evidence_refs.append(_evidence_from_observation(observation))
    落 cost_records(latency_ms) 每轮
final: 把 final assistant content 解析成 diagnosis dict(对齐 build_diagnosis 入参)→ build_diagnosis() 包
       → _derive_session_status(evidence_refs, missing, hard_failure, partial) → _persist_diagnosis
```

## Decision: LLM 输出如何对齐 build_diagnosis

LLM 终轮返回结构化 JSON(content 是 JSON 串,含 `root_cause_candidates`/`recommended_actions`/
`confidence`)。本层 parse → 调 `build_diagnosis(incident=, evidence_refs=, memory_hints=,
recommended_actions=..., )` 包出完整 schema。`_build_root_cause_candidates`/`_build_action_proposals`
不再调(LLM 产出),**删除**。

## Decision: 置信度护栏(保留 _score_confidence)

LLM 输出 `confidence.score` 与 `_score_confidence(evidence_chain, candidates)` 取 `max`,标 `degraded`
字段(LLM < 护栏)。信任边界内数值校验,不可省。`_confidence_for_observation`/`_score_confidence`/
`_confidence_level` 保留。

## Decision: _build_session_plan 处置

**保留**作 provider 不可达的降级路径(child 1 `ProviderUnavailable` 时回退到关键词 plan +
`build_diagnosis` 关键词根因),保证 needs_human/partial 语义不崩。`# ponytail: 保留关键词 plan 作
provider 降级回退,避免 provider 挂时整 session failed;大脑稳定后可删`。

## Decision: 删 synthetic adapter

`hermes/service_main.py:585-631` 四个 `_synthetic_*_adapter` 删 —— tool-use 路径下 MCP URL 未配置
应是 `partial` 缺口(像 `_topology_adapter` 现状那样),不是造假证据。adapter None 时 `_observe_tool`
已有 `_missing_observation` 走 skipped —— 沿用。

## Decision: diagnosis_trace 表 + cost latency

- `incident_store._SCHEMA_SQL` 加 `diagnosis_trace` 表(ADR-0005 §决策 2 字段):
  `id PK / session_id TEXT / step_index INTEGER / tool_name TEXT / tool_args_json TEXT /
   observation_ref TEXT / duration_ms INTEGER / model TEXT / input_tokens INTEGER /
   output_tokens INTEGER / trace_collected_at REAL`(无 FK,trace 可独立查)。
- `_ensure_*` 不需要(diagnosis_trace 是全新表,CREATE TABLE IF NOT EXISTS 即兼容);加 `add_diagnosis_trace` 方法 + 模块级 wrapper。
- `cost_guard._SCHEMA_SQL` 加 `latency_ms INTEGER`;`__init__` 后补 `_ensure_cost_columns`(套 try ALTER);
  `record_cost:183` 加 `latency_ms: int | None = None` 入参 + 写列。

## Decision: provider 注入

`run_diagnosis_session` 新增 `provider` 参数(ProviderConfig 或 ScriptedProvider,duck-typed 有
`chat_with_tools`);`hermes/service_main.start_diagnosis_session:136` 用 `dp.load_from_env()` 注入。
降级路径(provider=None 或抛 ProviderUnavailable)走 `_build_session_plan` 关键词回退。

## Tradeoffs

- tool-use loop glue ~30 行在此层,不在 provider 层(provider 单步)—— 边界干净,代价是本文件变长。
- 保留关键词 plan 作降级回退 —— 多留 ~80 行死代码直到大脑稳定;`# ponytail:` 标 ceiling。
- LLM 输出 parse 失败 → 走关键词回退降级,不崩 failed(状态机兜底)。
- trace 表无 FK:CRC 复现按 session_id 查即可,trace 是调试数据非合规事实(ADR-0005 §决策 1 分层)。

## Open items

- `find_similar_case_profiles` 精确签名检索对"未见故障"无命中 —— 首期空库也跑通(prompt 里无 hints)。
  泛化相似度后置(statement 已记)。
- LLM 输出 JSON parse 的容错:content 非 JSON → 降级关键词回退 + logger.warning。
- `max_turns` 默认值:沿用 configmap 无此 env,本层硬编默认(如 6),`# ponytail:` 标。

## Rollout / Rollback shape

- 改 `incident_diagnosis.py`(run loop + 删 2 关键词函数 + provider 注入 + trace 埋点)、
  `incident_store.py`(新表 + add_diagnosis_trace)、`cost_guard.py`(latency 列 + record_cost)、
  `hermes/service_main.py`(删 synthetic + provider 注入)、`tests/test_incident_diagnosis.py`(改 ~12-15 用例)、
  新 `tests/test_diagnosis_trace.py`/或并入 test_incident_store.py。
- rollback:`incident_diagnosis`/`incident_store`/`cost_guard`/`service_main` 四文件回滚;child 1 provider 独立保留。
- schema 变更向后兼容(CREATE IF NOT EXISTS + ALTER idempotent),旧库无痛升级。