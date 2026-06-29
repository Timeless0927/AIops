# Implement: Thin LLM tool-use rewrite + diagnosis_trace + cost latency

> Active task: `06-29-llm-tooluse-rewrite`(已 start)。依赖 child 1。
> 改动分 4 阶段:存储 → 编排重写 → 注入 → 测试。每阶段验证可跑即跑。

## Step 1 — `diagnosis_trace` 表 + `add_diagnosis_trace`(`toolsets/incident_store.py`)

- [ ] `_SCHEMA_SQL` 末尾加新表(ADR-0005 §决策 2 字段):
      `CREATE TABLE IF NOT EXISTS diagnosis_trace (id INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id TEXT NOT NULL, step_index INTEGER NOT NULL, tool_name TEXT NOT NULL,
       tool_args_json TEXT, observation_ref TEXT, duration_ms INTEGER, model TEXT,
       input_tokens INTEGER, output_tokens INTEGER, trace_collected_at REAL NOT NULL);`
      + 索引 `idx_diagnosis_trace_session ON diagnosis_trace(session_id, step_index)`。
- [ ] `IncidentStore.add_diagnosis_trace(self, *, session_id, step_index, tool_name,
      tool_args, observation_ref, duration_ms, model, input_tokens, output_tokens,
      collected_at=None) -> int`:走 `_execute_write` 写一行(JSON 化 tool_args)。
- [ ] 模块级 wrapper `add_diagnosis_trace(...)`(转 `_STORE`),风格对齐模块级 `add_evidence:1299`。
- [ ] 新 `tests/test_incident_store.py` 用例:`test_diagnosis_trace_round_trip` 写一行读回字段对齐。

## Step 2 — `cost_records.latency_ms` 列 + `record_cost` 入参(`toolsets/cost_guard.py`)

- [ ] `_SCHEMA_SQL` cost_records 末尾加 `latency_ms INTEGER`(新库直接有)。
- [ ] `CostGuardDB.__init__` 后加 `_ensure_cost_columns()`:`for col,def in {"latency_ms":"INTEGER"}:
      try ALTER ADD COLUMN except OperationalError: pass`(套 incident_store 范式)。
- [ ] `record_cost:183` 加 `latency_ms: int | None = None` kw 入参 + INSERT 列补 `latency_ms`。
- [ ] 既有 `test_cost_guard.py`(若有)验 0 回归 + 新增断言 latency_ms 落库。

## Step 3 — tool-use 重写 `toolsets/incident_diagnosis.py`

- [ ] 新增常量 `LLM_TOOLUSE_VERSION = "incident_diagnosis/llm-tooluse-v1"`;`COLLECTOR_VERSION:15`
      改指 LLM_TOOLUSE_VERSION(`# 关键词回退路径仍可单独标,见下`)。
- [ ] 新增 `_LLM_TOOLS = [...]`:4 个 tool 的 OpenAI function schema(name/description/parameters),
      parameters 字段对齐 `_build_tool_args:226` 给每工具构造的入参(query/selector/argv/time range 等)。
      tool name:`query_metrics`/`query_logs`/`run_k8s_read`/`get_service_topology`(与 adapter 字典一致)。
- [ ] 新增 `_build_tooluse_system_prompt(incident, memory_hints, runbook_hints) -> str`:
      system prompt 含角色/任务/可用工具简述/按 alert-type 检索的 runbook 段(`skills/sre/runbooks/<type>/` 读 README)
      /similar case hints(`find_similar_case_profiles` 命中才拼)/输出格式约束(JSON:root_cause_candidates/
      recommended_actions/confidence.score)。
- [ ] 新增 `_run_llm_tooluse_session(incident, adapters, provider, store, *, max_turns=6) -> list[observation] +
      final_assistant_content`:循环 child 1 `chat_with_tools`,收 tool_calls → `_observe_tool`(复用)+
      `_collect_evidence`(复用)+ `add_diagnosis_trace`(新)+ 回灌 tool result message → 收 final JSON。
      usage 记 cost_records(latency_ms)。`# ponytail: max_turns 硬编 6,大脑稳定后应 env 化`。
- [ ] 改 `run_diagnosis_session`:`provider` 新参(缺省 None)。provider 非 None → 走 `_run_llm_tooluse_session`;
      provider=None 或抛 `ProviderUnavailable`/JSON parse 失败 → 回退现有关键词路径(`_build_session_plan`+
      `_build_root_cause_candidates`/`_build_action_proposals`)**保留**作降级。
      两条路径产 evidence_refs,后段 `build_diagnosis`+`_derive_session_status`+`_persist_diagnosis` 共用。
- [ ] LLM final JSON → `_diagnosis_from_llm(content)`:parse 成 root_cause_candidates/recommended_actions/confidence;
      parse 失败 → 走关键词回退 + `logger.warning`。
- [ ] 置信度护栏:`build_diagnosis` 前取 `max(llm_score, _score_confidence(evidence_chain, candidates))`,
      若 `llm_score < guard` 标 `degraded`(diagnosis dict 加 optional flag)。

## Step 4 — `hermes/service_main.py` 注入 + 删 synthetic

- [ ] `start_diagnosis_session:136` `run_diagnosis_session(...)` 调用加 `provider=dp.load_from_env()`
      (try/except `ProviderUnavailable` → provider=None 走降级路径,logger.warning)。
- [ ] 删 `_synthetic_metrics_adapter`/`_synthetic_logs_adapter`/`_synthetic_k8s_read_adapter`(:585-631);
      4 个真 adapter 的 "MCP URL 未配置" 分支改为返回 `partial` envelope(像 `_topology_adapter:413` 那样),
      不造假证据。
- [ ] import `hermes.diagnosis_provider as dp`。

## Step 5 — 测试改面

- [ ] `tests/test_incident_diagnosis.py`:21 用例中 ~12-15 改面:注入 `ScriptedProvider`(child 1)
      脚本编排(先 tool_call 跑一遍→回灌→final JSON),把对 `_build_root_cause_candidates`/
      `_build_action_proposals` 关键词产出的断言 → 改为对 LLM 输出 diagnosis 的断言(provider 脚本
      明示 root_cause)。保留状态机用例(needs_human/partial/diagnosed)断言不变。
- [ ] 新 `tests/test_diagnosis_llm_tooluse.py`(或并 test_incident_diagnosis):ScriptedProvider 走通
      2-turn tool-use → 四路 evidence 落库(RecordingStore.assert add_evidence 被调)、diagnosis_trace ≥1 行、
      cost_records.latency_ms 有值。provider None → 关键词回退。provider 抛 ProviderUnavailable → 降级 partial。
- [ ] `tests/test_incident_evidence_collection.py`:确认 `_collect_evidence` 仍被 tool-use 路径调,0 回归。
- [ ] pytest 全绿,排除已知 pre-existing env 缺依赖。

## Step 6 — 验证 + commit

- [ ] `pytest tests/test_incident_diagnosis.py tests/test_diagnosis_provider.py tests/test_incident_evidence_collection.py
      tests/test_incident_store.py tests/test_cost_guard.py tests/test_diagnosis_llm_tooluse.py -q` 全绿。
- [ ] 状态机用例(needs_human/partial/diagnosed)0 回归。
- [ ] grep 确认 `_synthetic_*` 已删、`COLLECTOR_VERSION` 迁 `llm-tooluse-v1`。
- [ ] commit「feat(hermes): thin LLM tool-use diagnosis brain + diagnosis_trace + cost latency (ADR-0003 child 2)」。

## Review gates / Rollback points

- Step 1-2(存储)commit 一次「feat(store): add diagnosis_trace table + cost_records.latency_ms」。
- Step 3-4 commit 一次「feat(hermes): LLM tool-use rewrite of run_diagnosis_session」。
- Step 5 commit 测试改面(可并入上)。
- rollback:回滚 `incident_diagnosis`/`incident_store`/`cost_guard`/`service_main` 4 文件;schema 向后兼容 0 风险。

## Notes

- `build_diagnosis` schema 不变 —— writeback 口径依赖。
- 执行边界:模型产 mutation 仍是 action proposal,经 Gateway,Hermes 内不执行(不变)。
- 关键词回退路径保留:`# ponytail: 保留关键词 plan 作 provider 降级回退,provider 挂时不崩 failed;大脑稳定后删`。
- runbook/similar case 先空库跑通(prompt 无 hints 也合法)。