# Design: incident_diagnosis tolerate empty/unreachable backends (A-3)

Parent: 06-25-adr0005-issue-a-evidence-closure

## 改动边界

单文件、单函数为主:`toolsets/incident_diagnosis.py` 的 `_derive_session_status`(`:591-603`)。
不动 `_is_hard_failure`、`TERMINAL_FAILURE_CODES`、`_collect_evidence`、adapter、`run_diagnosis_session`
主流程(`:124` 仍照常累积 `hard_failure` 标志,只是该标志不再无条件等于 failed)。

## 现状逻辑(改前)

```python
def _derive_session_status(evidence_refs, missing_evidence, hard_failure, has_partial_observation=False):
    if hard_failure:
        return "failed"            # ← 一票否决:任一路 backend_unavailable 即整体 failed
    if not evidence_refs:
        return "needs_human"
    if missing_evidence or has_partial_observation:
        return "partial"
    return "diagnosed"
```

问题:`hard_failure` 跨四路 OR 累积(`run_diagnosis_session:124`),只要 metrics/k8s_read/topology
任一路后端不可达(envelope `error_code=backend_unavailable`),即便 logs 路成功有 evidence,session 仍 failed。

## 目标逻辑(改后)

把 `hard_failure` 从「一票否决」降级为「与 evidence 完整度结合判定」:

```python
def _derive_session_status(evidence_refs, missing_evidence, hard_failure, has_partial_observation=False):
    if not evidence_refs:
        # 完全无现场:有终端失败 → 仍需人工接手(产物已兜底写入),否则照旧 needs_human
        return "needs_human"
    if hard_failure or missing_evidence or has_partial_observation:
        # 有部分现场但采集不全(含某路终端失败) → partial
        return "partial"
    return "diagnosed"
```

要点:
- `evidence_refs` 为空 → `needs_human`(无论是否 hard_failure)。这吸收了「全路 backend_unavailable 且无产出」
  的旧 failed 场景,改判 needs_human —— 复用现有语义,下游零改动,且 `_persist_diagnosis` 已无条件写产物。
- 有 `evidence_refs` 但出现 hard_failure / 缺失 / partial → `partial`。这正是「一路不可达 + 其他路有数据」的目标。
- 全部成功无缺失 → `diagnosed`(不变)。
- `failed` 不再由 `_derive_session_status` 主动返回;`run_diagnosis_session:144-145` 的非法状态兜底仍可产 failed
  (`if status not in SESSION_STATES: status = "failed"`),保留 failed 作为防御性兜底,不删 `SESSION_STATES` 中的 failed。

## 为什么不动 `_is_hard_failure` / `TERMINAL_FAILURE_CODES`

`hard_failure` 标志本身语义正确(确实有一路终端失败),改的是「如何由它推导 session 状态」。保留标志可让
`partial` 与「干净的 partial(纯空匹配)」在未来需要时仍可区分(标志仍在 observation/step 里)。最小且可逆。

## 数据流与下游影响

- Hermes `service_main.py:157` 把 `session["status"]` 原样回 HTTP;`:247` 原样进 writeback payload。
- Gateway `diagnosis_writeback.py:54-55` 只校验 status 非空字符串,不校验取值 → 复用现有 `partial`/`needs_human`
  无协议风险。
- Console/Feishu 消费 status:本改动**不引入新值**(仍在 `SESSION_STATES` 5 值内),且把原本的 failed 改成
  语义更准确的 partial/needs_human,下游展示更合理,无需改下游。

## 兼容性 / 语义对齐(显式破坏点)

`test_diagnosis_session_backend_unavailable_fails_controlled`(`tests/test_incident_diagnosis.py:767-795`)
当前构造 metrics adapter 返回 `status=failed`+`error_code=BACKEND_UNAVAILABLE`,断言 session `failed`。
按新语义:若该测试场景其他路有 evidence → 应改断言为 `partial`;若该场景四路均无 evidence → 改断言为 `needs_human`。
需读该用例实际构造再定改法(implement 阶段确认),改名为反映新语义(如 `..._degrades_not_fails`)。

## 测试策略

1. 更新上述破坏用例。
2. 新增「一路 backend_unavailable + 其他路成功」→ `partial`。
3. 新增/复用「全路 hard_failure + 无 evidence」→ `needs_human`。
4. 回归:`diagnosed`(`:165`)、现有 partial(`:516,619,736`)、needs_human(`:744`)、
   evidence 落库(`test_incident_evidence_collection.py`)全绿。

## 回滚

单函数改动,`git revert` 或还原 `_derive_session_status` 即可;无数据迁移、无 schema 变更、无配置变更。
