# Diagnosis Session Status Derivation

> Hermes 诊断 session 的最终状态由 `_derive_session_status`
> (`toolsets/incident_diagnosis.py:591`) 从四路 evidence 采集结果推导,是
> Hermes → writeback → Console/Feishu 的**跨层契约**。核心规则:**单路后端不可达
> 不再一票否决整个 session 成 `failed`** —— 有现场就降级为 `partial`,无现场降级为
> `needs_human`,`failed` 只留作非法状态兜底。

---

## Scenario: session status 推导与空/不可达后端容错

### 1. Scope / Trigger

- Trigger: 跨层契约变更 —— `session["status"]` 经 `hermes/service_main.py:247` 进
  writeback payload,`apps/aiops_k8s_gateway/diagnosis_writeback.py` 透传不校验取值,
  最终被 Console `apps/aiops_console/.../incident-detail.js:112` 消费。改变状态推导
  会影响所有下游消费方。
- 历史背景:dev-external 真实后端 smoke 下,metrics/k8s_read/topology 任一路连不上
  (envelope `error_code=backend_unavailable`)即把整个 session 判 `failed`,即便 logs
  路有 evidence。这误导运维以为诊断链路坏了,且评测集现场被丢弃。

### 2. Signatures

```python
# toolsets/incident_diagnosis.py:27
SESSION_STATES = {"running", "diagnosed", "partial", "needs_human", "failed"}

# toolsets/incident_diagnosis.py:28
TERMINAL_FAILURE_CODES = {"backend_unavailable", "connector_offline", "timeout"}

# toolsets/incident_diagnosis.py:584
def _is_hard_failure(observation: dict) -> bool:
    # observation status=="failed" 且 audit.error_code ∈ TERMINAL_FAILURE_CODES

# toolsets/incident_diagnosis.py:591
def _derive_session_status(
    evidence_refs: list[dict],
    missing_evidence: list[dict],
    hard_failure: bool,
    has_partial_observation: bool = False,
) -> str
```

### 3. Contracts

推导优先级(改后,自上而下短路):

| 条件 | 返回 status | 语义 |
|---|---|---|
| `not evidence_refs` | `needs_human` | 完全无 non-memory 现场;诊断产物已兜底写入,待人工接手 |
| `hard_failure or missing_evidence or has_partial_observation` | `partial` | 有部分现场但采集不全(含某路终端失败) |
| 其余(有 evidence、无缺失、无 partial) | `diagnosed` | 完整诊断 |

- `failed` **不再由本函数主动返回**;仅 `run_diagnosis_session:144-145`
  `if status not in SESSION_STATES: status = "failed"` 作非法状态防御性兜底。
- `hard_failure` 标志仍由 `run_diagnosis_session:124` 跨四路 `or` 累积,语义不变
  (确有一路终端失败),改的只是「如何由它推导 session 状态」。
- 不引入 `SESSION_STATES` 之外的新值 —— 引入新值必须先检查下游 writeback/Console/Feishu
  消费方。

### 4. Validation & Error Matrix

| observation 情形 | observation status | 是否 hard_failure | 对 session 的影响 |
|---|---|---|---|
| envelope `failed` + `error_code∈TERMINAL_FAILURE_CODES` | failed | 是 | 有 evidence→partial;无 evidence→needs_human |
| adapter 为 None | skipped | 否 | 进 missing_evidence |
| adapter 抛异常(无 error_code) | failed | 否 | 单步不落 evidence,不影响整体判定 |
| envelope `succeeded` 但 0 匹配 | partial | 否 | has_partial_observation→partial |

### 5. Good/Base/Bad Cases

- Good(diagnosed):四路均 succeeded 有 evidence,无缺失 → `diagnosed`。
- Base(partial):metrics `backend_unavailable` + logs succeeded 有 evidence → `partial`。
- Bad(needs_human):四路全 `backend_unavailable`,无任何 evidence_refs → `needs_human`
  (**不再** `failed`)。

### 6. Tests Required

`tests/test_incident_diagnosis.py`:
- `test_diagnosis_session_one_backend_unavailable_with_evidence_is_partial` —
  断言 `session["status"] == "partial"`。
- `test_diagnosis_session_all_backends_unavailable_needs_human` —
  断言 `status == "needs_human"` 且 `evidence_chain == []`。
- `test_diagnosis_session_backend_unavailable_degrades_not_fails`(原
  `..._fails_controlled` 改名)— 单路 metrics failed、其余 None、无 evidence →
  `needs_human`;保留 `steps[0].status == "failed"` 与 confidence 断言。
- 回归:`diagnosed`(:165)、partial(:516,619,736)、needs_human(:744)不受影响。

`tests/test_incident_evidence_collection.py`:
- 单步 failed observation 跳过落库行为(`_collect_evidence:669`
  `if not incident_id or status == "failed": return`)不得被破坏。

### 7. Wrong vs Correct

#### Wrong(一票否决)
```python
def _derive_session_status(evidence_refs, missing_evidence, hard_failure, has_partial_observation=False):
    if hard_failure:
        return "failed"          # 单路后端不可达 → 整个 session failed,丢弃其他路现场
    ...
```

#### Correct(结合 evidence 完整度)
```python
def _derive_session_status(evidence_refs, missing_evidence, hard_failure, has_partial_observation=False):
    if not evidence_refs:
        return "needs_human"     # 无现场,但产物已兜底写入,待人工
    if hard_failure or missing_evidence or has_partial_observation:
        return "partial"         # 有现场但不全(含某路终端失败)
    return "diagnosed"
```

---

## Common Mistakes

### Common Mistake: PodCrashLooping fallback misses approval-required action

**Symptom**: Split Gateway -> Hermes end-to-end smoke succeeds, but
`recommended_actions` contains only read-only actions for a `PodCrashLooping`
alert.

**Cause**: Fallback action proposal matching only looks for lower-case symptom
tokens in the incident text and evidence summaries. Alert names are lowered
without word splitting, so `PodCrashLooping` becomes `podcrashlooping`; matching
only `crash loop` or `crashloopbackoff` misses it.

**Fix**: `_build_action_proposals` must treat `crashlooping` as a crashloop
mutation-advice trigger and still set `approval_required=True` and
`execute_automatically=False`.

**Prevention**: When adding or changing common Alertmanager alert names, include
both Kubernetes condition vocabulary (`CrashLoopBackOff`) and alert-rule names
(`PodCrashLooping`) in the fallback trigger tests.

### Common Mistake: 把「后端可达但空」当成 failed

**Symptom**: 后端正常但查询 0 行,session 被判 failed。

**Cause**: 混淆「后端不可达(terminal failure)」与「后端可达但无数据(partial)」。

**Fix**: 0 匹配的 envelope 是 `status=partial`(`incident_diagnosis.py:462-467`),
`error_code` 为空,不进 `TERMINAL_FAILURE_CODES`,不构成 hard_failure。

**Prevention**: 只有 envelope 显式 `status=failed` + `error_code∈TERMINAL_FAILURE_CODES`
才是 hard_failure;adapter 抛错(无 error_code)、adapter 缺失(skipped)都不是。
