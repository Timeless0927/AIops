# Replay eval harness + 10 incident fixtures

Parent: `06-29-adr0003-diagnosis-brain`。依赖 child 2(`06-29-llm-tooluse-rewrite`)。
代码侧可先合,fixture 运营延后并行借 Issue A(dev-external 真后端采)+ Issue B(真根因回填)。

## Goal

ADR-0003 §37 验收硬门槛:人工固化 ≥10 个真实历史 incident(告警 + 当时 metrics/logs/k8s 现场 + 事后确认真根因),
让 Hermes 回放,对比诊断与真相的命中率。本 child 产出回放 harness(代码侧)+ 10 个 fixture(运营侧)。
V1 验收线是"省人力"(on-call 初步排查负担显著下降);"比人准"是北极星,不作 V1 验收线。

## Requirements

### 代码侧
- `tests/replay_incident.py`(harness):加载固化 fixture(incident meta + 冻结 evidence 行 + 事后确认真根因 case_profile)
  → 调 `run_diagnosis_session` → 对比输出 `root_cause_candidates[0].cause` vs `truth.root_cause_category`,
  按 `root_cause_category` 带容差打分(类别相等满分;兄弟/上位带容差系数;confidence≥阈值加成)→ 报告命中率。
- harness 用 child 1 `ScriptedProvider` 模拟当时大脑所见(不依赖真 provider),对齐字段用现成 `root_cause_category`(incident_store:190)。
- 先提交 1-2 个**示范** fixture(可借 child 2 脚本证据合成),验证 harness 自洽。

### 运营侧
- `tests/fixtures/incidents/<incident-id>/`:每个含 `incident.json` + `evidence/*.json` + `truth.json`(真根因+类目+有效处置)。
- 10 个真实 fixture 借 Issue A 的 dev-external 真后端跑真告警采证据 + Issue B 真根因回填;
  按 `incident_store.list_evidence` schema 存以对齐。运营为人工成本,逐个诚实固化。

## Constraints

- fixture 必须是真实故障,不得为凑数合成(ADR-0003 验收标准的诚实要求)。
- harness 不绑定真 provider;脚本可注入。
- 打分按 `root_cause_category` 带容差,不靠字符串相等(ADR-0005 §决策 4)。
- 不微调诊断模型、不做 self-improve 反馈(ADR-0003 非目标)。

## Acceptance Criteria

- [ ] `pytest tests/test_replay_incident.py -q` 绿:harness 自检 1 个示范 fixture 跑通,打分逻辑正确。
- [ ] `python3 tests/replay_incident.py` 全量 fixture 跑出命中率报告 ≥ ADR-0003 §V1 验收线(省人力初步排查负担下降)。
- [ ] `tests/fixtures/incidents/` 下 ≥10 个真实 incident 目录。
- [ ] 回放结论写回 ADR-0003 验收锚点。

## Verification

`pytest tests/test_replay_incident.py -q` + `python3 tests/replay_incident.py` 命中率报告 +
`ls -1 tests/fixtures/incidents/ | wc -l` ≥ 10。
rollback:删 `tests/replay_incident.py` + `tests/fixtures/incidents/` 目录,不影响 child 2 代码。