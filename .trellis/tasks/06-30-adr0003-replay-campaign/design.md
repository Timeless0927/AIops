# design.md — fixture 导出器(ADR-0003 replay campaign #4)

> 单一职责:从 live 跑通的 incident + session 读 store,冻结成 replay harness 可消费的 fixture。
> 不是产线代码,是运营工具,落 `tests/export_incident.py`(与 `tests/replay_incident.py` 同目录)。

## 契约(Explore 已钉死,见 task research 笔记)

三文件 fixture(`tests/fixtures/incidents/<incident_id>/`):

- `incident.json`:`incident_id`/`session_id`/`alert_name`/`namespace`/`cluster`/`service`/`summary`/`time_range`/`synthetic:false`
- `evidence/NN_<source>.json`:每文件一观察 = `{tool,status:"succeeded",summary,payload,namespace,service,ref_id,tool_args}`
- `truth.json`:`root_cause_category`/`final_root_cause`/`key_evidence_refs`/`effective_actions`/`synthetic:false`/`recorded_prediction:{category,cause,confidence,level}`

## 数据源 → fixture 字段映射

| fixture 字段 | store 来源 | 备注 |
|---|---|---|
| incident_id | `incidents.id`(`get_incident`) | |
| session_id | **CLI `--session-id`** | 不在 incidents 表;trace 按 session_id 查 |
| alert_name/namespace/cluster/service/summary | `incidents.*` | |
| time_range | `incidents.created_at`+`resolved_at` → ISO8601 interval | store 是 unix epoch float |
| synthetic | 硬编码 `false` | real fixture |
| evidence[].tool / tool_args | **`diagnosis_trace.tool_name` / `tool_args`**(`list_diagnosis_trace(session_id)`) | 按 step_index 排序 |
| evidence[].summary / payload | `incident_evidence.summary` / `payload`(`list_evidence(incident_id)`) | |
| evidence[].namespace/service | 从 incident 拷 | incident_evidence 无此列 |
| evidence[].ref_id | `diagnosis_trace.observation_ref` 兜底 `ev_<source>_<tool>_<idx>` | |
| evidence[].status | 硬编码 `succeeded` | incident_evidence 无 status 列 |
| truth.root_cause_category | `case_profile.root_cause_category`(`get_case_profile`) | 真根因(人回填) |
| truth.final_root_cause | `case_profile.final_root_cause` | |
| truth.key_evidence_refs / effective_actions | `case_profile.*`(JSON 字符串 parse) | |
| truth.recorded_prediction | `incidents.diagnosis_json` parse → `root_cause_candidates[0]` | **大脑当时产出**,非真根因;缺失则省略(harness fallback 到 root_cause_category) |

## trace ↔ evidence 链接(关键难点)

`diagnosis_trace`(按 session_id)有 `tool_name`/`tool_args`/`observation_ref`;
`incident_evidence`(按 incident_id)有 `summary`/`payload`/`source_type`/`source_ref`。
两表无 FK,但生产 `_collect_evidence` 写 evidence 时把 `observation.evidence_ref` 同时落进
trace 的 `observation_ref` 和 evidence 的 `source_ref`——这是可靠链接键。

**纯位置配对的坑**(check agent 抓到):trace 每步都写一行,但 `_collect_evidence` 对
`failed` status 提前返回不写 evidence 行。一个 failed middle step 会让后续所有位置配对错位 1,
payload 配错工具 + 末尾假性 missing。所以位置不能当主键。

链接策略(**observation_ref 优先,顺序兜底**,两遍):
1. pass 1:keyed match——trace `observation_ref` ↔ evidence `source_ref`,匹配的 (idx→evidence) 记下并标记 consumed
2. pass 2:位置兜底——只对**没有 observation_ref** 的 trace step,用未 consumed 的 evidence 行按序配;有 ref 但没匹到的 step 不抢行(真 missing)
3. 没匹到 evidence 的 trace step → payload `{}` + summary 空 + 标 `_trace_only_missing_evidence`

这样 failed middle step 的 keyed step 仍正确配对,位置兜底只服务真无 ref 的边角。

## CLI

```
python3 tests/export_incident.py <incident_id> --session-id <sid> [--out tests/fixtures/incidents]
```

- 必填 `incident_id` + `--session-id`
- 读 live hermes 的 `/data/aiops/incidents.db`(导出器在 pod 内跑,或本地跑指向同 DB)—— 部署形态:kubectl exec hermes 跑,或 host 跑指向 PVC 拷出的 DB。**最省事:pod 内跑**,DB 路径 `/data/aiops/incidents.db` 写死 + `--db` 覆盖。
- 输出目录默认 `tests/fixtures/incidents/<incident_id>/`,已存在则 `--force` 覆盖

## 不做

- 不改产线(`incident_diagnosis.py` / `run_diagnosis_session` / provider)。
- 不做 live 采证(采证是 ADR-0005 Issue A 已落地的产线行为,导出器只读结果)。
- 不做 fixture 去重/版本化(运营期手管,够了)。
- 不脱敏:fixture 进 git,但 payload 已经过产线 `redact_k8s_output`/`redact_sensitive_text`(ADR-0005 决策 3),导出器不二次脱敏。导出前人工 spot-check 一遍。

## 验收

- 导出器单测:用临时 DB 插一个 incident + trace + evidence + case_profile,跑导出器,assert 三文件结构 + 字段映射对。
- 导出真实 incident 后,`python3 tests/replay_incident.py --root <out>` 能加载并回放该 fixture(harness 不报错、real_count 计入)。
- harness 子集 76 passed 不回归(导出器不动产线 + harness)。