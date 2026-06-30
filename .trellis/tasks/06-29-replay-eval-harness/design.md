# Replay eval harness + 10 incident fixtures — design

Parent: `06-29-adr0003-diagnosis-brain`。依赖 child 2(`06-29-llm-tooluse-rewrite`)。

## 目标与边界

产出**回放 harness(代码侧)**,让固化 fixture(incident meta + 冻结 evidence + 事后确认真根因 case_profile)
对 Hermes `run_diagnosis_session` 回放,按 `root_cause_category` 带容差打分出命中率报告。
运营侧的 ≥10 个真实 fixture **延后并行**(借 Issue A 真后端采证 + Issue B 真根因回填),与本 child 代码解耦;
本 child 代码侧先把 harness 跑自洽,只内置 **1-2 个合成示范 fixture** 证明 harness 端到端通,不为凑数合成真 fixture。

合闸边界(已与用户确认):代码侧先合,运营侧延后。"V1 命中率报告 ≥ 省人力验收线"这条 AC 在真 fixture 凑齐前
按 0/N 占位报告跑通 harness,真值由运营侧后续补 fixture 后填上。代码侧不再阻塞于真后端采集流程就绪。

## 关键约束(evidence 已确认)

- fixture 是真实故障(运营侧硬约束,ADR-0003 验收的诚实要求);本 child 代码侧的示范 fixture 明确标注 `synthetic: true`,不混入真 fixture 计数。
- harness 不绑定真 provider;用 child 1 `ScriptedProvider` 注入"当时大脑所见"(`hermes/diagnosis_provider.py:227`)。
- 打分按 `root_cause_category` 带容差,**不靠字符串相等**(ADR-0005 §决策 4)。
- 不微调诊断模型、不做 self-improve(ADR-0003 非目标)。

## 核心架构决策:category 对齐方式

### 问题(本 design 的最大难点)

模型输出 `root_cause_candidates[i].cause` 是**自由文本**(prompt schema 只要 `cause/confidence/evidence_refs`,
`toolsets/incident_diagnosis.py:196-200`;`_diagnosis_from_llm` 原样 parse,`:377`)。
truth 在 fixture/case_profile 里是 `root_cause_category` 标签(如 `connection_pool_exhaustion`,
`tests/test_gateway_case_profile.py:105`)。两者维度不同——自由文本 cause 无法直接对齐标签。

仓里**没有** category 词汇表、层级或容差矩阵。ADR-0003 / ADR-0005 只给了"按类目带容差"
的方向,没给具体口径。

### 决策(已与用户确认)

**改 child 2 已归档的 prompt JSON schema,要求 candidate 多返回一个 `category` 字段;harness 用一张手维护的
容差矩阵打分。** 模型自己提交 category 标签,比对两侧标签,不靠脆弱文本对齐。

理由:最诚实——打分锚点是模型自己声称的类别不是事后猜措辞;比"后置文本→category 映射"健壮(后者
'人猜模型会怎么说',换措辞就漏判);比二分相等保留了 ADR-0005 点名的容差(兄弟/上位类部分分)。

代价:动 child 2 已归档的 `incident_diagnosis.py` prompt schema,需带回归测试(见 implement.md gate)。

### category 词汇表与容差矩阵(harness 自带,手维护)

harness 内置一个 `CATEGORY_TAXONOMY` 和 `TOLERANCE` 矩阵。初始小集合(随真 fixture 运营补全):

```
TOLERANCE 系数(对角线满分 1.0):
  类别相等                     → 1.0
  同桶兄弟(共享 tolerance bucket,见 group)→ 0.5
  否则                          → 0.0
```

兄弟用 `CATEGORY_GROUPS` 上位桶归类(如 `resource_pressure_memory`↔`resource_pressure_cpu` 同属
`resource_pressure` 桶),兄弟间给 `0.5`,列在桶外给 `0.0`。truth 始终是叶 category(review 时
确认上位类分支不可达,已剪除——保留 `PARENT` 是死代码)。系数集中在一个常量块,运营侧新增 category 时一眼可见要补哪条。
confidence≥阈值的加分项(`+0.1`,封顶 1.0)保留为可选加成,不进硬门槛线以免刷分。分数线(命中谓词):单 fixture 命中当
`score ≥ 0.5`;命中率 = 命中 fixture 数 / N。fallback(模型输出 `undifferentiated` 而 truth 是具体类)直接 0.0 不蹭分。

容差矩阵是**手维护产物**,不是模型学出来的;新增真 fixture 引入新 truth category 时,运营者同步扩词汇表
+ 矩阵(harness 起 `--validate-taxonomy` 子命令做自检,见下)。

## 数据流

```
tests/fixtures/incidents/<id>/
  incident.json   — incident meta(alert_name/summary/namespace/service),喂 run_diagnosis_session
  evidence/*.json — 冻结 evidence rows(list_evidence schema: source_type/source_ref/summary/
                    payload_json/window_*/collected_at),harness 装成 ScriptedProvider 脚本
  truth.json      — {root_cause_category, final_root_cause, key_evidence_refs, effective_actions}
  (可选 synthetic:true 标记)

harness(replay_incident.py)
  load fixture → 构造 ScriptedProvider(scripts 按 evidence rows 排好工具调用回放)
               → 构造 FakeAdapter 把冻结 evidence 当成 tool observation 灌回
               → await run_diagnosis_session(incident, provider=..., metrics_adapter=..., ...)
               → 取 session["diagnosis"]["root_cause_candidates"][0] 的 cause+category
               → score(candidate.category, truth.root_cause_category, confidence) via TOLERANCE
               → {fixture_id, predicted_category, truth_category, score, hit, degraded} 逐条 + 汇总命中率
```

### provider 注入细节(evidence 已确认)

- `run_diagnosis_session(incident, *, provider, metrics_adapter, logs_adapter, topology_adapter, k8s_read_adapter, incident_store)`(`incident_diagnosis.py:441`)。
- adapter 是 `Callable[[dict], Awaitable[ToolEnvelope]]`(`:36`),fixture 的 evidence rows 装成按 tool 名匹配的
  `FakeAdapter` 复用现成范式(`tests/test_incident_diagnosis.py` 的 `FakeAdapter`)。
- `ScriptedProvider(scripts)` 的每条 script 是 raw chat-completions dict,模型先 tool_calls(含 `query_metrics`
  等),harness 把对应 FakeAdapter 的 observation 设为 tool result content 灌回,模型再 final stop message(content
  是含 `category` 的新 schema JSON)。脚本顺序对齐 evidence 文件顺序——**示范 fixture 的脚本就是回放大脑的
  一次完整 tool-use 轨迹**,不是真采证。
- `incident_store` 传 None 或 FakeIncidentStore;harness 不依赖真库(`add_diagnosis_trace` 不在 harness 验收
  线,harness 只读 diagnosis 输出,不验 trace——trace 验收归 parent smoke)。

## 复用(钉死不动)

- child 1 `ScriptedProvider`(`hermes/diagnosis_provider.py:227`)+ `_parse_chat_response` raw body 形状。
- child 2 `run_diagnosis_session` + `_diagnosis_from_llm`(JSON parse)+ `_apply_confidence_guardrail`(phase 4
  保持)`+ build_diagnosis` schema 不变。
- `FakeAdapter`/`TopologyFacadeAdapter` 范式(`tests/test_incident_diagnosis.py`)。

## 改动面

| 文件 | 改动 | 风险 |
|---|---|---|
| `toolsets/incident_diagnosis.py` | prompt JSON schema 加 `category` 字段(`:196-200` 的 instr 串)| 中:动 child 2 归档文件,需带回归 |
| `tests/replay_incident.py` | 新建:harness 主体 | — |
| `tests/test_replay_incident.py` | 新建:harness 自检 | — |
| `tests/fixtures/incidents/<2 个示范>/` | 新建:incident.json + evidence/*.json + truth.json (`synthetic:true`)| — |

不动的接口:`build_diagnosis` 输出 schema(markdown/confidence.score/level)、状态机、writeback。
candidate 新增 `category` 是**加法**,不破现有 `cause/confidence/evidence_refs` 字段——`_compose_diagnosis`
原样透传 candidates(`:432`),不读 category,writeback 口径不受影响。

## 兼容 / 回滚

- candidate 多个 `category` 字段:writeback/Gateway 不读该字段(harness-only),向下兼容,不触发契约迁移。
- 回滚:删 `tests/replay_incident.py` + `tests/test_replay_incident.py` + `tests/fixtures/incidents/` 示范目录;
  child 2 prompt 那行 schema instr 回滚一处字符串。`incident_diagnosis` 主路径不受影响。

## 验收锚点(代码侧,本 child)

- `pytest tests/test_replay_incident.py -q` 绿:harness 自检 1 个示范 fixture 跑通,打分逻辑正确(命中/容差/报各分支)。
- `python3 tests/replay_incident.py --validate-taxonomy` 自检容差矩阵无悬挂 category。
- `python3 tests/replay_incident.py`(零真 fixture 时)跑出占位报告 + 不崩,报告格式可承接后续真 fixture。
- 改 prompt schema 后 `pytest tests/test_incident_diagnosis.py tests/test_diagnosis_provider.py -q` 0 回归(回归 gate)。
- 示范 fixture 不混入真 fixture 计数(报告里 `synthetic` 标注 + 总命中率分真人/合成两栏,或合成示侢单列不进真 fixture 总分)。

运营侧 AC(延后,不计入本 child 代码合闸):`tests/fixtures/incidents/` 下 ≥10 个真 fixture + 命中率报告 ≥ ADR-0003 §V1 验收线。