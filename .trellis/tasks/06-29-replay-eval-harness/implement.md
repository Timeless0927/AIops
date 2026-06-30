# Replay eval harness — implement

复杂任务。parent: `06-29-adr0003-diagnosis-brain`。链路依赖 child 2(`06-29-llm-tooluse-rewrite`,已归档)。
代码侧先合;≥10 真实 fixture 运营延后并行,本计划不覆盖运营采集。

## 执行顺序(ordered checklist)

### Step 1 — child 2 prompt schema 加 category 字段(回归 gate 前置)
- [ ] `toolsets/incident_diagnosis.py:196-200` 的 prompt JSON instr 串,candidate 对象加 `"category":"<root_cause_category 标签>"`,并在 schema instr 里给出候选标签集提示(指向 harness 的 `ROOT_CAUSE_CATEGORIES`,但 prompt 不强绑闭集——模型可输出未列类目,harness 视为 `undifferentiated` 兜底)。
- [ ] 文字说明:category 是模型自评的根因类别标签,供回放按类目带容差打分。
- [ ] **回归 gate**(动归档文件必跑):`pytest tests/test_incident_diagnosis.py tests/test_diagnosis_provider.py tests/test_incident_evidence_collection.py -q` 全绿。已有用例断言 `candidate["cause"]`(test:568/952/981)不受影响——加法字段,不破。
  - 若回归红:多半是 schema instr 串拼接出错,检查 f-string;`_diagnosis_from_llm` 不需改(透传 parsed dict)。

### Step 2 — 容差矩阵 + 词汇表(harness 自带常量)
- [ ] `tests/replay_incident.py` 顶部定义 `ROOT_CAUSE_CATEGORIES`(初始小集合,见 design.md)+ `CATEGORY_GROUPS`(上位桶)+ `TOLERANCE` 系数(兄弟 0.5 / 上位 0.7 / 不等 0.0)+ `CONFIDENCE_BONUS`(0.1,封顶 1.0,可选)。
- [ ] `--validate-taxonomy` 子命令:遍历 `ROOT_CAUSE_CATEGORIES`,核对每个 category 都在某个 `CATEGORY_GROUPS` 桶里或自成一桶,无悬挂;exit 非 0 若有悬挂。供运营侧新增 truth category 时自检。

### Step 3 — 打分函数 score()
- [ ] `score(predicted_category, truth_category, confidence) -> {score, hit, reason}`:
  - 相等 → 1.0;同桶兄弟 → 0.5;否则 0.0(上位类分支不可达,truth 始终是叶,已剪除)。
  - confidence≥阈值(0.6)且 score>0 → `score += 0.1`(封顶 1.0)。
  - `hit = score >= 0.5`。
  - predicted 为空/`undifferentiated` 且 truth 非 `undifferentiated` → 直接 0.0(不让兜底蹭分);predicted==truth==undifferentiated 走 exact。
- [ ] 单测覆盖每个分支(在 test_replay_incident.py 直接调 score,不依赖 fixture)。

### Step 4 — fixture loader
- [ ] `load_fixture(dir_: Path) -> dict`:读 incident.json + evidence/*.json + truth.json;遇 `synthetic:true` 标记隔离。
- [ ] `list_fixtures(root="tests/fixtures/incidents") -> list[Path]`:目录扫描,每个子目录一个 fixture。

### Step 5 — provider + adapter 装配(回放大脑一次 tool-use 轨迹)
- [ ] fixture 的 evidence rows → `ScriptedProvider` scripts(模型先 tool_calls,observation 灌回,再 final stop message 含新 schema JSON)+ per-tool `FakeAdapter`(按 tool 名匹配 observation,复用 test_incident_diagnosis 范式)。
- [ ] 调 `await run_diagnosis_session(incident, provider=..., metrics_adapter=..., logs_adapter=..., topology_adapter=..., k8s_read_adapter=..., incident_store=FakeIncidentStore())`。
- [ ] 取 `session["diagnosis"]["root_cause_candidates"][0]` 的 `cause` + `category`(category 缺失兜底 `undifferentiated`)。

### Step 6 — harness 主体 + 报告
- [ ] 遍历 fixtures → 逐条 `{fixture_id, predicted_category, truth_category, score, hit, degraded, synthetic}` → 汇总命中率(合成示例子列不进真 fixture 总分)。
- [ ] 零真 fixture 时跑出占位报告(0/0 真人命中率)+ 示范 fixture 自洽演示,不崩。
- [ ] CLI:`python3 tests/replay_incident.py [--root DIR] [--validate-taxonomy]`,JSON / 文本报告到 stdout,exit 0。

### Step 7 — 自检测试
- [ ] `tests/test_replay_incident.py`:
  - score() 全分支(相等/兄弟/上位/不等/兜底蹭分/置信加成)。
  - 1 个合成示范 fixture 跑通:provider 轨迹 → diagnosis → 打分 → hit=True,字段齐全。
  - `--validate-taxonomy` 对悬挂 category 报错。
- [ ] `pytest tests/test_replay_incident.py -q` 绿。

### Step 8 — 示范 fixture(2 个,合成)
- [ ] `tests/fixtures/incidents/synthetic-memory-pressure/`:incident.json(memory 高告警)+ evidence/*.json(4 路:metrics/logs/topology/k8s_read)+ truth.json(`root_cause_category=resource_pressure_memory`,`synthetic:true`)。provider 脚本对齐这条 tool-use 轨迹。
- [ ] `tests/fixtures/incidents/synthetic-cert-expiry/`:同理,`root_cause_category=certificate_expiry`,演示一个兄弟类容差场景(score 0.5 不是 1.0 的两个类放一起做对照可选;本示例 main 做"相等命中"以保 self-check 强,容差分支留给 score 单测)。

### Step 9 — check / spec 写回
- [ ] 跑 `pytest tests/test_replay_incident.py tests/test_incident_diagnosis.py tests/test_diagnosis_provider.py -q` 全绿(回归)。
- [ ] 跑 `python3 tests/replay_incident.py --validate-taxonomy` + `python3 tests/replay_incident.py` 占位报告。
- [ ] 跨 child 结论写回 ADR-0003 验收锚点(本 child 代码侧绿灯;运营侧真 fixture + 命中率线留 parent 收尾)——由 parent 阶段统一写回 ADR-0003,本 child 只在 prd/design 标注运营依赖未齐。

## 验证命令

```bash
pytest tests/test_replay_incident.py -q
pytest tests/test_incident_diagnosis.py tests/test_diagnosis_provider.py tests/test_incident_evidence_collection.py tests/test_incident_store.py -q   # 回归 gate
python3 tests/replay_incident.py --validate-taxonomy
python3 tests/replay_incident.py
ls -1 tests/fixtures/incidents/ | wc -l        # 当前预期 2(合成),真 fixture 运营后补到 ≥10
```

## 风险与回滚

- **动 child 2 归档文件**(`incident_diagnosis.py` prompt):最大风险点。是加法字段不破 schema,但回归 gate 必跑。回滚点:Step 1 那处 instr 字符串回滚即可隔离本 child。
- 示范 fixture 脚本与真 fixture 计数混淆:报告分真人/合成两栏 + `synthetic:true` 双保险。
- 容差矩阵悬挂:运营侧新增 truth category 忘扩矩阵→`--validate-taxonomy` 兜住。
- harness 回滚:删 `tests/replay_incident.py` + `tests/test_replay_incident.py` + `tests/fixtures/incidents/synthetic-*`,child 2 主路径与 trace/cost 存储不受影响。

## review gates(before task.py start 之外)

- Step 1 后:回归 gate 必须绿才进 Step 2。
- Step 7 后:harness 自检绿 + 回归绿,才写示范 fixture(Step 8)。
- 全绿后:`trellis-check` 走一遍,再由 parent 做端到端 smoke + ADR-0003 验收写回。