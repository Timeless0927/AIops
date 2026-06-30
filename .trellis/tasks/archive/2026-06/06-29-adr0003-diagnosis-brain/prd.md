# ADR-0003 diagnosis brain — thin LLM tool-use (parent)

## Goal

把 `toolsets/incident_diagnosis.py` 的关键词规则引擎替换为**薄 LLM tool-use 编排**(ADR-0003):
四个证据 MCP adapter 注册为 LLM 工具,模型现场读证据、自主决定调用顺序、推理根因、输出结构化诊断。
这是短期诊断大脑的本体;ADR-0005 剩余的 D(出境日志)/E(trace 埋点)随大脑共切入点落地。
本 parent 统筹三个 child,parent 负责跨 child 端到端绿灯,不直接实现。

## Why now

- ADR-0005 Issue A 端到端已闭环(parent+3 child 全 archived),Issue B/C 在动手前已是 done 状态。
- ADR-0005 落地路径剩余的 D/E 都硬绑 ADR-0003 大脑(LLM 层不存在即无 trace/token/出境数据),
  故 ADR-0003 是真正的短期主线,也是 ADR-0005 收尾的前置。
- 现状:`incident_diagnosis.py` 885 行纯规则、0 LLM 调用;provider 调用层从 0;
  `diagnosis_trace` 表 + `cost_records.latency_ms` 从 0;回放集从 0。

## Confirmed Facts(三路 Explore 交叉印证)

- **要替换的规则函数**:`_build_session_plan:193`(关键词选采集计划)、`_build_action_proposals:621`、
  `_build_root_cause_candidates:779`、`_confidence_for_observation:574`+`_score_confidence:817`+_confidence_level:827`。
- **要保留/复用(钉死不动)**:四个 MCP HTTP adapter(`hermes/service_main.py:366-426`)、
  `_collect_evidence:655`+`_redact_payload:695`+`_evidence_window:713`、`_observation_from_envelope:450`+`_as_mapping:516`、
  `_derive_session_status:591` 状态机、`build_diagnosis:34` schema+`render_markdown:153`、
  writeback 全链路(`service_main._writeback_diagnosis_artifacts:239`+`diagnosis_writeback:65`+HMAC)、
  `incident_store.add_evidence:477`+`record_incident_diagnosis:620`+`find_similar_case_profiles:864`。
- **provider env 接缝已备**(deploy 侧无需改):`deploy/k8s/configmap.yaml:21-24`
  `AIOPS_MODEL_PROVIDER/NAME/BASE_URL`、`deploy/k8s/secret.example.yaml:14` `AIOPS_MODEL_API_KEY`。
  `requirements-runtime.txt` 只有 `httpx>=0.27`,不引 openai SDK,自写 tool-use 循环。
- **存储已为回放设计**:`incident_store` 有 `collector_version`/时间窗 epoch/`root_cause_category`,
  `_ensure_*_columns:258` 提供 idempotent ALTER 范式供新表/新列复用。
- runbook 实体已存:`skills/sre/runbooks/{certificate-expiry,high-memory,node-not-ready,pod-crashloop,pvc-full}/`。

## submodule 决策(已定)

删 `hermes-agent` 实测是大型 legacy 解耦:`runtime/hermes_gateway.py:45 from hermes_cli.gateway import run_gateway`
是生产 Gateway 入口,约 15 个 toolsets 走 `sys.path.insert(hermes-agent)`+`except ImportError` 兜底,
`Dockerfile.aiops:26-27` 专门 COPY+pip install,一批测试硬依赖 `hermes-agent/` 路径。
**与大脑本体无关**(`incident_diagnosis.py` 不 import submodule)。submodule 解耦**另起独立任务,不进本 parent**。

## Child Tasks

| Child | 范围 | 依赖 | 验收信号 |
|---|---|---|---|
| 06-29-diagnosis-provider-layer | provider 调用层(httpx + OpenAI-compatible tool-use)+ 出境日志 + ScriptedProvider 测试件 | 无 | 脚本 tool-use 跑通→final diagnosis;httpx 超时→降级;出境日志含 base_url;`grep litellm\|openai hermes/` 空 |
| 06-29-llm-tooluse-rewrite | `incident_diagnosis.py` tool-use 重写 + `diagnosis_trace` 表 + `cost_records.latency_ms` | child 1 | 四路 evidence 落库 + trace ≥1 行 + latency 有值;状态机(needs_human/partial/diagnosed)0 回归 |
| 06-29-replay-eval-harness | replay harness 代码侧 + ≥10 fixture 运营(借 Issue A 真后端采 + Issue B 真根因回填) | child 2 | harness 跑 10 fixture 命中率 ≥ ADR-0003 §V1 验收线 |

依赖链:child 1 → child 2 → child 3(code 先合,fixture 运营延后并行)。

## Parent Acceptance Criteria(跨 child 端到端)

- [ ] 三个 child 各自 AC 满足并归档。
- [ ] FakeProvider 全链路 smoke:1 个示范 fixture → ScriptedProvider → 跑通一次会话,四路 evidence 落库 +
      `diagnosis_trace` ≥5 行 + `cost_records.latency_ms`>0。
- [ ] provider 不可达(fake endpoint shutdown)→ `_derive_session_status` 走 needs_human/partial,不崩溃。
- [ ] 状态机测试 0 回归;出境日志可见;全仓 `pytest -q` 绿(排除已知 pre-existing env 缺依赖)。
- [ ] 跨 child 结论写回 ADR-0003 验收锚点 / spec。

## Embedded Decisions(已采 Plan 推荐)

- `_score_confidence` 保留为 LLM 输出置信度的**下限护栏**:LLM 输出 score 取
  `max(llm_score, _score_confidence(...))` 并标 `degraded`。信任边界内数值校验,不可省。
- `COLLECTOR_VERSION` 从 `keyword-v1` 迁 `llm-tooluse-v1`(回放可区分新旧证据采)。
- 案例记忆按精确 `incident_signature` 调 `find_similar_case_profiles` 先接,泛化后置;命中才拼 `optional_memory_hints`,
  不可否决现场证据。

## Out of Scope

- submodule 解耦(另起独立任务)。
- Langfuse 引入(ADR-0005 §决策 3 后置)。
- Console 表单(child profile 回填 UI,后置)。
- 微调 / self-improve / 强化学习闭环(ADR-0003 非目标)。