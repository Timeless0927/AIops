# Implement: incident_diagnosis tolerate empty/unreachable backends (A-3)

Parent: 06-25-adr0005-issue-a-evidence-closure

## 验证命令

```bash
pytest tests/test_incident_diagnosis.py -q
pytest tests/test_incident_evidence_collection.py -q
pytest tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py tests/test_incident_store.py -q
```

## 有序 checklist

### 1. 确认改动前真实形态
- [ ] 1.1 读 `tests/test_incident_diagnosis.py:767-795` 的 `test_diagnosis_session_backend_unavailable_fails_controlled`,
      确认它构造几路 adapter、其他路是否产 evidence_refs —— 决定新断言是 `partial` 还是 `needs_human`。
- [ ] 1.2 读 `:744-764` needs_human 用例,确认全路缺失场景断言不被本改动破坏。

### 2. 改 `_derive_session_status`(`toolsets/incident_diagnosis.py:591-603`)
- [ ] 2.1 调整推导顺序为:无 evidence_refs → `needs_human`;有 evidence_refs 且(hard_failure / missing / partial)→ `partial`;
      否则 `diagnosed`。`failed` 不再由本函数主动返回(保留 `:144-145` 非法状态兜底)。
- [ ] 2.2 不改 `_is_hard_failure`、`TERMINAL_FAILURE_CODES`、`run_diagnosis_session:124` 累积逻辑。
- [ ] 2.3 函数 docstring/注释更新,点明「hard_failure 不再一票否决,需结合 evidence 完整度」。

### 3. 更新与新增测试
- [ ] 3.1 改 `test_diagnosis_session_backend_unavailable_fails_controlled`:按 1.1 结论改断言(partial 或 needs_human),
      改名为反映新语义(如 `test_diagnosis_session_backend_unavailable_degrades_not_fails`)。
- [ ] 3.2 新增用例:metrics 路 backend_unavailable + logs 路成功有 evidence → `status=="partial"`。
- [ ] 3.3 新增/确认用例:四路全 hard_failure 且无 evidence_refs → `status=="needs_human"`。
- [ ] 3.4 回归确认 `diagnosed`(`:165`)、现有 partial(`:516,619,736`)用例未受影响。

### 4. 质量验证
- [ ] 4.1 跑验证命令三条全绿。
- [ ] 4.2 `_collect_evidence` 单步 failed 跳过落库行为未变(`test_incident_evidence_collection.py:94` 通过)。
- [ ] 4.3 grep 确认无其他地方硬依赖「backend_unavailable → session failed」语义。

### 5. 收尾
- [ ] 5.1 dispatch `trellis-check` 校验 spec 合规 + 全量测试。
- [ ] 5.2 spec 更新:本次状态语义结论写入 `.trellis/spec/`(诊断状态推导契约)。
- [ ] 5.3 Phase 3.4 提交 `toolsets/incident_diagnosis.py` + 测试改动。

## 风险 / 回滚

| 步骤 | 风险 | 回滚 |
|---|---|---|
| 2.1 | 改顺序误伤 diagnosed/needs_human 边界 | 还原 `_derive_session_status` 单函数 |
| 3.1 | 测试断言改错方向(partial vs needs_human) | 以 1.1 实际 adapter 构造为准 |

## 验收锚点(回 parent)

本 child 完成后,parent 端到端 smoke 中「单路后端不可达不再整体 failed」一项即满足;logs/metrics 有数据
依赖兄弟 child(stdout-logging / servicemonitor)。
