# Research: 诊断会话状态推导与空后端容错改动点

- **Query**: 摸清 AIOps 诊断会话状态推导逻辑,为「让 incident_diagnosis 在后端为空/稀疏时不直接 failed、且仍写 analysis」做准备
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Files Found

| File Path | Description |
|---|---|
| `toolsets/incident_diagnosis.py` | 诊断编排核心:`run_diagnosis_session`、`_derive_session_status`、`_observe_tool`、`_collect_evidence`、`_persist_diagnosis`、`TERMINAL_FAILURE_CODES`、`SESSION_STATES` |
| `toolsets/incident_store.py` | SQLite 存储:`incident_evidence`/`incident_analysis` 表 DDL、`add_evidence`、`upsert_analysis`、`record_incident_diagnosis` 及模块级 wrapper |
| `hermes/service_main.py` | Hermes HTTP 服务:调用 `run_diagnosis_session`、四路 adapter、writeback、`status="failed"` + `error_code="backend_unavailable"` 兜底 envelope |
| `apps/aiops_k8s_gateway/diagnosis_writeback.py` | Gateway 侧 writeback:`apply_diagnosis_writeback` → `store.record_incident_diagnosis` |
| `hooks/alert_webhook.py` | Legacy webhook 路径:唯一调用 `upsert_analysis`(写 `incident_analysis` 表)的业务代码 |
| `aiops/contracts/errors.py` | `ErrorCode` enum:`TIMEOUT`/`BACKEND_UNAVAILABLE`/`CONNECTOR_OFFLINE` 等 |
| `tests/test_incident_diagnosis.py` | 状态推导主测试(diagnosed/partial/needs_human/failed) |
| `tests/test_incident_evidence_collection.py` | 证据落库行为测试(succeeded/partial/skipped 落,failed 不落) |

---

### 1. `_derive_session_status` 完整逻辑

定义:`toolsets/incident_diagnosis.py:591-603`

```python
def _derive_session_status(
    evidence_refs, missing_evidence, hard_failure, has_partial_observation=False,
) -> str:
    if hard_failure:
        return "failed"
    if not evidence_refs:
        return "needs_human"
    if missing_evidence or has_partial_observation:
        return "partial"
    return "diagnosed"
```

输入(均在 `run_diagnosis_session` 内累积):
- `evidence_refs` — 成功产出 `evidence_ref` 的 observation 列表(`incident_diagnosis.py:97,113-114`)。
- `missing_evidence` — 没有 `evidence_ref` 的步骤记录(`:116-123`)。
- `hard_failure` — 是否出现「终端失败」observation(`:124`,见下)。
- `has_partial_observation` — 是否有任一步 `status == "partial"`(`:125`)。

优先级:hard_failure > 无任何 evidence(needs_human)> 有缺失或 partial(partial)> diagnosed。

**`hard_failure` 触发**:`_is_hard_failure`(`incident_diagnosis.py:584-588`):
```python
def _is_hard_failure(observation):
    if observation["status"] != "failed":
        return False
    error_code = str(observation.get("audit", {}).get("error_code") or "")
    return error_code in TERMINAL_FAILURE_CODES
```
即:observation 必须 `status=="failed"` 且 `audit.error_code` 落在 `TERMINAL_FAILURE_CODES` 集合内,才把整个 session 推成 `failed`。任意一路命中即 `hard_failure=True`(`:124` 用 `or` 累积)。

**`TERMINAL_FAILURE_CODES`**:`incident_diagnosis.py:28`
```python
TERMINAL_FAILURE_CODES = {"backend_unavailable", "connector_offline", "timeout"}
```

注意 observation 的 `status` 字段取值:
- adapter 为 `None` → `_missing_observation(..., status="skipped")`(`:410,418-423`),`audit` 里**无** `error_code`,只有 `missing_reason` → 不构成 hard_failure。
- adapter 抛异常 → `_missing_observation(..., status="failed")`(`:414`),但 `audit` 里也**无** `error_code` → **不构成 hard_failure**(error_code 为空,不在集合内)。这是关键:adapter 抛错只让该步 skipped/无证据,不直接判 failed。
- adapter 正常返回 envelope → `_observation_from_envelope`(`:450-480`),`status = data.get("status") or "failed"`,`audit` 透传 envelope 的 `audit`(含 `error_code`)。**只有走这条且 envelope 显式 `status=failed` + `error_code∈集合` 才 hard_failure**。

### 2. `run_diagnosis_session` 整体流程

定义:`toolsets/incident_diagnosis.py:77-150`

1. 初始化 `session`(status="running"),`evidence_refs=[]`(`:87-97`)。
2. `plan = _build_session_plan(incident)`(`:99`,关键词路由四路工具,`:193-212`)。
3. 循环每个 step(`:102-125`):
   - `observation = await _observe_tool(...)`(`:110`)。
   - `session["steps"].append(observation)`(`:111`)。
   - `await _collect_evidence(...)`(`:112`)— **此处写 `incident_evidence` 表**。
   - 有 `evidence_ref` → 进 `evidence_refs`;否则进 `missing_evidence`(`:113-123`)。
   - 累积 `hard_failure`、`has_partial_observation`(`:124-125`)。
4. `build_diagnosis(...)`(`:129-134`)— 纯内存构造 diagnosis dict(summary/root_cause/evidence_chain/confidence/markdown 等),**无论有无 evidence 都会构造**;无 evidence 时给 `insufficient non-memory evidence` 兜底 candidate(`build_diagnosis` `:50-58`)。
5. `status = _derive_session_status(...)`(`:138-143`);若不在 `SESSION_STATES` 则强制 `failed`(`:144-145`)。
6. `session["status"]=status`,追加 `state_transitions`(`:146-147`)。
7. **`await _persist_diagnosis(incident, diagnosis, incident_store)`(`:149`)— 无条件执行,与 status 无关**。

**关键纠正(与任务前提不符)**:在 `incident_diagnosis.py` 内,**failed 并不会跳过写 diagnosis**。`_persist_diagnosis`(`:716-728`)在状态推导之后**无条件**调用 `store.record_incident_diagnosis`,把 diagnosis 摘要写到 `incidents` 行的 `diagnosis_*` 字段。同理 `_collect_evidence` 也只对**单个 observation** 为 `failed` 时跳过该条证据(`:654` `if not incident_id or status == "failed": return`),而非整个 session。

**`incident_analysis` 表与本流程无关**:`run_diagnosis_session` / Hermes 路径**从不**调用 `upsert_analysis`。写 `incident_analysis` 表的唯一业务代码是 legacy `hooks/alert_webhook.py:620,673`(`_persist_incident_analysis_context` / `_attach_similar_case_recall`)。Hermes 诊断写的是 `incident_diagnosis`(即 `incidents.diagnosis_json` 等列,经 `record_incident_diagnosis` `incident_store.py:620-656`)。任务里说的「仍写 incident_analysis」需要先和调用方确认指代的是哪张表 / 哪个写入点(见 Caveats)。

### 3. Session status 取值枚举与语义

定义:`toolsets/incident_diagnosis.py:27`
```python
SESSION_STATES = {"running", "diagnosed", "partial", "needs_human", "failed"}
```
语义(由 `_derive_session_status` 推导 + `run_diagnosis_session` 使用):
- `running` — 初始态(`:90-91,93`)。
- `diagnosed` — 有 evidence 且无缺失/无 partial(成功诊断)。
- `partial` — 有 evidence 但存在缺失证据或 partial 步骤。
- `needs_human` — 完全无 non-memory evidence(`evidence_refs` 为空)。
- `failed` — 出现终端失败(`hard_failure`)或推导出非法状态。

注意:`SESSION_STATES` 仅在 `incident_diagnosis.py` 内定义,**没有**集中的 enum/contract;`aiops/contracts`、`aiops/domain` 内未发现 session_status 枚举。Hermes 把 `session["status"]` 原样回传 HTTP 响应(`service_main.py:157`)并原样塞进 writeback payload 的 `status`(`service_main.py:247`)。Gateway `validate_writeback_payload` 只校验 `status` 非空字符串,不校验取值(`diagnosis_writeback.py:54-55`),所以新增/复用现有 status 值在协议层无强约束。

### 4. 四路 evidence 的「空/skipped/partial/unavailable」表达

四路来源常量:`incident_diagnosis.py:13` `EVIDENCE_SOURCES = {"metrics","logs","topology","k8s_read"}`。工具→source 映射 `_source_type_for_tool`(`:553-561`)。

observation `status` 与「空/不可用」的映射:
- **adapter 缺失(None)**:`_missing_observation(status="skipped")`,summary/missing_reason 为 `"<tool> adapter unavailable"` 等(`:410,442-447`);`audit` 无 `error_code`。→ 进 `missing_evidence`,**不** hard_failure。
- **adapter 抛异常**:`_missing_observation(status="failed")`,reason=`"<tool> adapter raised ..."`(`:414`);`audit` 无 `error_code`。→ `_collect_evidence` 跳过该条证据(`:654`);**不** hard_failure(error_code 为空)。
- **envelope status=failed + error_code∈TERMINAL_FAILURE_CODES**:hard_failure=True → session=failed。Hermes 在后端真不可用时正是走这条:`service_main.py:582` 兜底 envelope `{"status":"failed","error_code":"backend_unavailable",...}`;Prometheus `toolsets/prometheus_query.py:325,344`、Loki `toolsets/loki_query.py:568,587`、k8s_read `toolsets/k8s_read.py:708,718`、Gateway command `apps/aiops_k8s_gateway/command_service.py:75 connector_offline` 都会产出这些 error_code。
- **k8s_read 0 匹配**:envelope `status=succeeded` 但 `resource_match_count==0` → 降级为 `partial`(`:462-467`),confidence 0.25(`_confidence_for_observation` `:574-576`)。这是「后端可达但结果为空」的 partial 表达。
- **无 evidence_ref**:`_first_evidence_ref` 返回 None(`:533-542`)→ 进 `missing_evidence`,`missing_reason` 来自 envelope errors/summary(`_missing_reason_from_envelope` `:545-552`)。

**code 与 TERMINAL_FAILURE_CODES 的关系**:只有 `backend_unavailable`/`connector_offline`/`timeout` 三个 error_code 会把 session 推 failed;`skipped`、空匹配 partial、adapter 抛错(无 error_code)都不会。所以「后端为空但可达(0 行/空 series)」目前已落 partial 或 missing_evidence,不会 failed;真正会 failed 的是后端**不可用**(backend_unavailable 等)。

### 5. incident_analysis / incident_evidence 数据结构与写入点

**`incident_evidence` 表**:DDL `incident_store.py:162-175`(列:incident_id, source_type, source_ref, summary, payload_json, window_start_ts, window_end_ts, collected_at, collector_version, confidence)。
- 写入:`add_evidence`(`incident_store.py:480-521`,INSERT 见 `:499-519`);模块 wrapper 经 `_resolve_store` 反查(`incident_diagnosis.py:630-637`)。
- 诊断侧写入点:`_collect_evidence`(`incident_diagnosis.py:640-677`),对 succeeded/partial/skipped 落库,`failed` 跳过(`:654`),payload 先脱敏(`_redact_payload` `:680-695`)。

**`incident_analysis` 表**:DDL `incident_store.py:177-188`(主键 incident_id;列:symptoms_json, likely_scope, suspected_root_causes_json, supporting_evidence_json, missing_evidence_json, next_best_actions_json, confidence, last_analyzed_at)。
- 写入:`upsert_analysis`(`incident_store.py:543-`,INSERT…ON CONFLICT upsert `:563-579`);模块 wrapper `:1336-1355`。
- 业务调用方:**仅** `hooks/alert_webhook.py:620`(`_persist_incident_analysis_context`)、`hooks/alert_webhook.py:673`(`_attach_similar_case_recall`)。诊断 session 不写此表。
- 读取方:`hooks/voice_context.py:152-163`(`get_analysis` → `render_context_summary`);Feishu `publish_incident_analysis_summary`。

**`incident_diagnosis`(诊断摘要,非独立表)**:`record_incident_diagnosis`(`incident_store.py:620-656`)UPDATE `incidents` 行 `diagnosis_summary/confidence/level/json/markdown/diagnosed_at`。诊断侧写入点 `_persist_diagnosis`(`incident_diagnosis.py:716-728`,无条件)。Gateway writeback 侧 `diagnosis_writeback.py:76`。

### 6. 现有测试

`tests/test_incident_diagnosis.py`:
- `:165` 断言成功路径 `session["status"] == "diagnosed"`。
- `test_diagnosis_session_needs_human_when_no_non_memory_evidence`(`:744-764`):四路 adapter 全 `None` → status `needs_human`,`evidence_chain == []`,所有 step 有 `missing_reason`。**这是现成的「空后端」场景**(adapter 缺失而非后端报错)。
- `test_diagnosis_session_backend_unavailable_fails_controlled`(`:767-795`):metrics adapter 返回 `status=failed` + `error_code=ErrorCode.BACKEND_UNAVAILABLE` → status `failed`,`confidence.level=="low"`。**这是要被改动语义影响的关键用例**:当前期望 failed,改成「容错」后此断言需要重新定义。
- 多处 partial 场景:`:516,619,736`(k8s 0 匹配 / topology partial / logs partial)。

`tests/test_incident_evidence_collection.py`:
- `test_succeeded_and_skipped_observations_persist_failed_does_not`(`:71-111`):验证 failed observation 不落 evidence;末尾 `:111` 断言 `session["status"] in {"partial","diagnosed","needs_human"}`。
- `:114-134` partial 低 confidence;`:137-158` 脱敏。

`tests/test_incident_store.py`:`:158` `upsert_analysis`、`:183-195` `record_incident_diagnosis` 落库测试。`tests/test_hermes_diagnosis_service.py`、`tests/test_incident_analysis_summary.py` 覆盖 Hermes service / analysis summary 渲染。

## Caveats / Not Found

- **任务前提需校正**:`run_diagnosis_session` 在 failed 时**并不跳过**写 diagnosis —— `_persist_diagnosis`(`incident_diagnosis.py:149`)无条件执行。所以「failed 时为何跳过写 analysis」在诊断代码里不成立。可能的真实诉求是:(a) failed 时下游/UI 视 diagnosis 无效,或 (b) 指 legacy `incident_analysis` 表(只由 `hooks/alert_webhook.py` 写,与 Hermes 流程独立)。建议向调用方确认「analysis」具体指 `incidents.diagnosis_json`(Hermes)还是 `incident_analysis` 表(webhook)。
- `SESSION_STATES` 无集中 contract enum;协议层(Gateway writeback)不校验 status 取值,改动 status 推导风险集中在 `incident_diagnosis.py` 自身 + 测试断言。
- 未发现 `run_diagnosis_session` 调用 `upsert_analysis` 的任何路径(grep 全仓确认)。

---

## 总结:最小改动点与约束

要实现「空/稀疏后端容错但仍持久化诊断产物」,最小改动集中在 **`toolsets/incident_diagnosis.py`** 一处函数 + 一个常量,核心是放宽 `failed` 的触发条件:

1. **`TERMINAL_FAILURE_CODES`(`:28`)/ `_is_hard_failure`(`:584-588`)/ `_derive_session_status`(`:591-603`)**:这是把后端不可用判 `failed` 的唯一来源。要让「空/稀疏」不直接 failed,应当:
   - 区分「后端不可用(真 failed)」与「后端可达但空(应为 partial/needs_human)」。当前空匹配已是 partial(`:462-467`)、adapter 缺失已是 needs_human/skipped,**真正会被这次改动影响的只有 envelope 显式返回 `error_code∈TERMINAL_FAILURE_CODES` 的情况**。
   - 若要「即便 backend_unavailable 也不直接 failed」,改 `_derive_session_status`:在 hard_failure 但仍有部分 evidence 时降级为 `partial`,无 evidence 时降级为 `needs_human`,仅在「全部四路都 hard_failure 且无任何 evidence/产物」时才保留 `failed`(或干脆改成 needs_human)。
2. **持久化已无需改动**:`_persist_diagnosis`(`:149`)本就无条件写 diagnosis;`build_diagnosis`(`:34-74`)在无 evidence 时也产出兜底结构。所以「仍写诊断产物」这一目标当前已满足,无需额外加写。
3. **若「analysis」确指 `incident_analysis` 表**:则需要在 Hermes 诊断流程新增 `upsert_analysis` 调用(复用 `incident_store.upsert_analysis` `:543`,字段 symptoms/likely_scope/suspected_root_causes/supporting_evidence/missing_evidence/next_best_actions/confidence),这属于新增写入点而非容错改动,需先确认范围。

**约束(别破坏现有语义)**:
- 保留 `diagnosed`(有 evidence、无缺失)与 `failed`(真终端失败)的既有边界;测试 `:165` 的 diagnosed、`:792` 的 failed 是契约。
- `test_diagnosis_session_backend_unavailable_fails_controlled`(`:767-795`)会因语义放宽而需要更新,这是预期的、需要显式与调用方对齐的破坏点,不要悄悄改。
- `_collect_evidence` 对单条 `failed` observation 跳过落库的行为(`:654`)是 evidence-collection 契约(`test_incident_evidence_collection.py:94`),session 级容错不应改这条单步规则。
- Hermes writeback 把 `session["status"]` 原样上报(`service_main.py:247`),Gateway/Console/Feishu 是下游消费者;若引入新 status 值需检查这些消费方,复用现有 `partial`/`needs_human` 最稳。
