# incident_diagnosis tolerate empty/unreachable backends (child A-3)

Parent: 06-25-adr0005-issue-a-evidence-closure

## Goal

修正 `run_diagnosis_session` 的 session 状态推导:**单路后端不可达不应一票否决整个 session 成 `failed`**。
当部分 evidence 路可达且有产出时,session 应 derive 成 `partial`(采集不全但有现场),而非 `failed`。
这让评测集即便在后端稀疏时也能沉淀诊断现场,避免误导运维以为诊断链路本身坏了。

## Confirmed Facts(已读码验证)

- 触发链路:Gateway webhook → Hermes service → `run_diagnosis_session`(`toolsets/incident_diagnosis.py:77`)。
- 一票否决发生在:`run_diagnosis_session:124` 跨四路 OR 累积 `hard_failure`;`_derive_session_status:597`
  第一行 `if hard_failure: return "failed"` —— 任一路 `error_code∈TERMINAL_FAILURE_CODES` 即整体 failed。
- `TERMINAL_FAILURE_CODES`(`:28`)= `{"backend_unavailable","connector_offline","timeout"}`。
- `_is_hard_failure`(`:584`):observation `status=="failed"` 且 `audit.error_code` 命中集合。
- 现状边界(**不要破坏**):
  - 后端可达但空(k8s 0 匹配)→ 已是 `partial`(`:462-467`)。
  - adapter 缺失(None)/ adapter 抛异常(无 error_code)→ 已是 `skipped`/`needs_human`,不 hard_failure。
  - 真正受本改动影响的**仅**「envelope 显式 `status=failed` + `error_code∈集合`」一种。
- 诊断产物持久化无需改:`_persist_diagnosis:149` 无条件写 `incidents.diagnosis_json`;
  `incident_analysis` 表不被 Hermes 路径写(范围已与用户确认:**只修状态语义,不补 analysis 表**)。

## Requirements

- 改 `_derive_session_status`:当存在 hard_failure 但**仍有部分 evidence_refs** 时,derive 成 `partial`
  而非 `failed`;当 hard_failure 且**完全无 evidence_refs** 时,derive 成 `needs_human`(有现场可人工)。
- 保留 `diagnosed`(有 evidence、无缺失、无 partial)与现有 `partial`/`needs_human` 语义边界。
- `_collect_evidence` 对单条 failed observation 跳过落库的行为(`:654`)**不动**(那是 evidence-collection 契约)。
- 不引入新的 session status 值(复用 `SESSION_STATES` 现有 5 值),避免触及 Console/Feishu/Gateway writeback 下游。
- 更新受影响测试 `test_diagnosis_session_backend_unavailable_fails_controlled`(`tests/test_incident_diagnosis.py:767-795`),
  这是显式的语义对齐点,需明确改断言而非悄悄改。
- 新增/补充测试:覆盖「一路 backend_unavailable + 其他路有 evidence → partial」「全路 backend_unavailable + 无 evidence → needs_human」。

## Resolved Decisions

- **单路后端不可达 + 其他路有 evidence → `partial`**(用户确认)。
- **`failed` 的保留边界**:全路 hard_failure 且无 evidence_refs 时 derive `needs_human`(有诊断产物可人工接手),
  `failed` 仅保留给「非法状态」兜底(`:144-145`)。理由:复用现有 needs_human 语义,下游消费方无需改;
  且 `_persist_diagnosis` 无条件写产物,needs_human 比 failed 更贴合「有现场待人工」的真实情形。

## Acceptance Criteria

- [ ] 一路 `backend_unavailable` + 其他路有 evidence_refs → `run_diagnosis_session` 返回 `status=="partial"`。
- [ ] 全四路 hard_failure 且无 evidence_refs → `status=="needs_human"`(不再 `failed`)。
- [ ] 现有 `diagnosed`(`tests/test_incident_diagnosis.py:165`)与 partial(`:516,619,736`)用例仍通过。
- [ ] `test_diagnosis_session_backend_unavailable_fails_controlled` 已按新语义更新(改名/改断言),
      明确反映「不再一票 failed」。
- [ ] `pytest tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py` 全绿。
- [ ] `_collect_evidence` 单步 failed 跳过落库行为未变(`test_incident_evidence_collection.py:94` 仍通过)。

## Out of Scope

- 不补 `incident_analysis` 表写入(用户已确认只修状态语义)。
- 不改 evidence 采集逻辑、不改 adapter、不改 `_collect_evidence` 单步规则。
- 不引入新 status 值,不动下游 writeback/Console/Feishu。
- stdout 日志、ServiceMonitor 是兄弟 child,不在本任务。
