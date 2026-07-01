# implement.md — fixture 导出器(ADR-0003 replay campaign #4)

## 执行 checklist

1. [ ] 写 `tests/export_incident.py`:
   - `load_live_incident(incident_id, session_id, db_path)` → 读 `get_incident` + `list_evidence` + `list_diagnosis_trace` + `get_case_profile`
   - `build_fixture(incident, evidence, trace, case_profile)` → 组装 incident.json dict + evidence 行 list + truth.json dict(按 design 映射表)
   - `write_fixture(out_dir, fixture, force)` → 落三文件(evidence/NN_<source>.json 按 step_index 排序编号)
   - CLI:`argparse` 接 `incident_id` positional + `--session-id` 必填 + `--db`(默认 `/data/aiops/incidents.db`)+ `--out`(默认 `tests/fixtures/incidents`)+ `--force`
   - main: `asyncio.run` 驱动 store async 调用
2. [ ] 写 `tests/test_export_incident.py`:
   - 临时 DB(tmp_path)插 incident + 2 trace + 2 evidence + case_profile(用 `IncidentStore` 真方法写)
   - 跑导出器,assert 三文件存在 + 字段映射(incident.synthetic=false、evidence[].tool=trace.tool_name、truth.root_cause_category=case_profile、recorded_prediction 来自 diagnosis_json)
3. [ ] 跑 `python3 -m pytest -q tests/test_export_incident.py tests/replay_incident.py tests/test_diagnosis_provider.py tests/test_diagnosis_llm_tooluse.py` 全绿
4. [ ] 集成验证:导出 9046e0e5 那个 session(回填 case_profile 后)→ `python3 tests/replay_incident.py --root <out>` 加载不报错 + real_count 计入

## 验证命令

```bash
python3 -m pytest -q tests/test_export_incident.py tests/replay_incident.py
# 集成(pod 内,case_profile 回填后):
kubectl -n aiops-dev exec deploy/aiops-hermes -- python3 /app/tests/export_incident.py 85dc3e03-... --session-id diagnosis-9046e0e5... --out /tmp/fixtures
```

## 回滚

导出器是新文件 + 新测,不碰产线。回滚 = 删 `tests/export_incident.py` + `tests/test_export_incident.py`。

## review gate

实现完跑 trellis-check(spec 合规:导出器不破 ADR-0003 薄编排 / ADR-0004 cost / ADR-0005 layering;只读 store)。