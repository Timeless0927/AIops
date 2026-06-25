# Research: Webhook → 诊断触发链路与 smoke 观测归因

- **Query**: 把 `POST /webhooks/alertmanager` 端到端触发链路查穿,定位 smoke 观测(session failed / incident_analysis 未写 / 只落 1 路 logs line_count:0)的成因与 A-3 改动落点
- **Scope**: internal
- **Date**: 2026-06-25

## 结论先行(TL;DR)

本次 smoke 实际走的是 **Hermes 路径**,不是 legacy `hooks/alert_webhook.py`。完整链路:

```
POST /webhooks/alertmanager
  → apps/aiops_k8s_gateway/main.py:615 (路由)
  → apps/aiops_k8s_gateway/alertmanager_webhook.py:129 process_payload
      → :154 _create_or_reuse_incident   (建 incident,成功)
      → :171 trigger_hermes_diagnosis_session  (HTTP POST 到 Hermes /diagnosis/sessions)
  → hermes/service_main.py:97 enqueue_diagnosis_session (HTTP 202 queued, 后台线程)
      → :180 _run_background_diagnosis → :118 start_diagnosis_session
          → :133 run_diagnosis_session(四个 HTTP adapter)
              → toolsets/incident_diagnosis.py:77 主编排
```

`session status=failed` 是 **`toolsets/incident_diagnosis.py` 的 diagnosis session 对象**的 status,由 `_derive_session_status`(`incident_diagnosis.py:591`)推出,经 Hermes 原样上报/writeback。

`incident_analysis` 表**两条路径都不写**——它只被 legacy `hooks/alert_webhook.py:620,673` 写。所以 smoke 里「incident_analysis 未写」不是 bug,而是 Hermes 路径**从设计上就不写这张表**;Hermes 路径写的诊断产物是 `incidents.diagnosis_json` 等列(经 writeback)。

A-3「空后端容错且仍写 analysis」的最小正确改动点落在 **`toolsets/incident_diagnosis.py`**(状态推导),`hooks/alert_webhook.py` 与本次 smoke 无关。

---

## Findings

### Files Found

| File Path | Description |
|---|---|
| `apps/aiops_k8s_gateway/main.py` | HTTP 路由:`/webhooks/alertmanager`(`:615`)、`/diagnosis/writeback`(`:405`) |
| `apps/aiops_k8s_gateway/alertmanager_webhook.py` | alertmanager ingress:`process_payload`、`_create_or_reuse_incident`、`trigger_hermes_diagnosis_session` |
| `hermes/service_main.py` | Hermes `/diagnosis/sessions` handler、`enqueue_diagnosis_session`、`start_diagnosis_session`、四路 adapter、`_failed_tool_envelope`、writeback |
| `toolsets/incident_diagnosis.py` | `run_diagnosis_session` + `_derive_session_status`(session status 真正产出点) |
| `apps/aiops_k8s_gateway/diagnosis_writeback.py` | Gateway 接收 Hermes writeback,写 `incidents.diagnosis_json` |
| `hooks/alert_webhook.py` | **legacy** 路径,唯一写 `incident_analysis` 表者,本 smoke 未触发 |

---

### 1. `/webhooks/alertmanager` handler 与诊断派发链

- 路由:`apps/aiops_k8s_gateway/main.py:615` `if route_path == "/webhooks/alertmanager"`,调用 `alertmanager_webhook.handle_http_request`(import 在 `main.py:36`)。
- ingress 主体:`alertmanager_webhook.py:129 process_payload`,注释明确写着 **“Persist alert ingress state and trigger Hermes without doing diagnosis.”**(`:130`)。
- 对每条 firing alert:
  - `_create_or_reuse_incident`(`:154,196-215`)→ `incident_store.create_incident`(建 incident,smoke 观测“incident 创建成功”对应这里)。
  - 生成 `session_id = f"diagnosis-{uuid4}"`(`:156`),记 `alert_fired` 事件(`:157-170`)。
  - **`trigger_hermes_diagnosis_session`(`:171-177`)**:这是诊断派发的唯一出口。
- `trigger_hermes_diagnosis_session`(`:246-269`):若 `AIOPS_HERMES_URL` 已设置,HTTP POST 到 `${HERMES_URL}${AIOPS_HERMES_DIAGNOSIS_PATH:-/diagnosis/sessions}`(`:254-259`),payload 含 `incident_id/session_id/source=alertmanager/alert/...`。若未设 URL 则 `{"status":"skipped"}`(`:255-256`)。

**结论**:Gateway 自身**不执行诊断**,把诊断异步派给 Hermes service。Gateway 进程内**不调用** `run_diagnosis_session`,也**不调用** legacy `hooks/alert_webhook.py`。

Hermes 接收端:`hermes/service_main.py:97 enqueue_diagnosis_session`(POST `/diagnosis/sessions`),立即返回 `202 queued`(`:177`)并 `_run_background_diagnosis`(`:175,180`)在后台线程跑 `start_diagnosis_session`(`:183,118`)→ `run_diagnosis_session`(`:133-139`),四路 adapter 为 `_metrics_adapter/_logs_adapter/_k8s_read_adapter/_topology_adapter`。

### 2. `session status=failed` 是哪张表/对象的 status

- 它是 **`toolsets/incident_diagnosis.py` 内 session dict** 的 `status` 字段(`incident_diagnosis.py:88-96` 初始化、`:146` 写入),由 `_derive_session_status`(`:591-603`)推出。
- **不是** Gateway 侧表的列。Gateway/incident 表没有 session_status 概念;Gateway 只有 incident 行的 `status`(firing/resolved)和 `diagnosis_*` 诊断列。
- session status 的去向(三处,均原样透传字符串,无落 DB 成独立 session 表):
  1. Hermes HTTP 响应 `{"service":"hermes","status":session["status"],...}`(`service_main.py:157`)。
  2. 内存缓存 `_DIAGNOSIS_SESSIONS[session_id]`(`service_main.py:141`)。
  3. writeback payload 的 `status` 字段(`service_main.py:247`)→ Gateway `apply_diagnosis_writeback` 把它写进 `investigate_end` timeline 事件的 detail(`diagnosis_writeback.py:82,87`),**不写进 incident 行的状态列**。

所以 smoke journal 里看到的 `status=failed` 来自 Hermes session 对象 / timeline 事件,根源是 `_derive_session_status` 命中 `hard_failure`。

### 3. failed 在哪一步 derive、为何没写 analysis

**failed 的产生(空/不可用后端)**:smoke 环境下四路 adapter 走 HTTP 分支(`AIOPS_*_MCP_URL` / `AIOPS_GATEWAY_URL` 已配置,指向 loki-ns observability,见 commit `984d66b`)。当某后端连不上或返回非 JSON:

- `_http_tool_adapter`(`service_main.py:426-444`)捕获 `OSError/URLError/TimeoutError/...` → `_failed_tool_envelope`(`:436`)。
- `_failed_tool_envelope`(`:574-583`)产出 `status="failed"` + **`audit.error_code="backend_unavailable"`**。
- 该 envelope 进 `_observe_tool` → `_observation_from_envelope`(`incident_diagnosis.py:450`),透传 `audit.error_code`。
- `_is_hard_failure`(`incident_diagnosis.py:584-588`)命中(`backend_unavailable ∈ TERMINAL_FAILURE_CODES` `:28`)→ `hard_failure=True`(`:124`)。
- `_derive_session_status` 第一条 `if hard_failure: return "failed"`(`:597-598`)→ session=failed,**即便其它路有 evidence 也被它一票否决**。

这解释了观测:metrics/k8s_read/topology 后端不可用 → 各自 `backend_unavailable` → hard_failure;只有 logs 一路连上 Loki 但返回空(`line_count:0`)。

**“只落 1 路 logs 且 line_count:0”**:`_collect_evidence`(`incident_diagnosis.py:640-677`)对 `status=="failed"` 的 observation 跳过落库(`:654`),所以三路 failed 的不进 `incident_evidence`;logs 那路 envelope `status` 非 failed(空结果通常 succeeded/partial),被落库,payload 即 `line_count:0`。这与 spec `hermes-agent/backend/logging-guidelines.md:119-132` 描述的「结构空 line_count:0」一致。

**“incident_analysis 未写” 的真正含义**:
- 走的是 Hermes 路径,而 Hermes 路径**从不调用 `upsert_analysis`**(全仓 grep 确认:`upsert_analysis` 调用方只有 `hooks/alert_webhook.py:620,673` 与测试)。所以 `incident_analysis` 表在本 smoke 下**永远不会被写**,与 session 是否 failed 无关。
- Hermes 路径写的诊断产物是 `incidents.diagnosis_json/diagnosis_summary/...`,经两条写入:
  1. Hermes 进程内 `_persist_diagnosis`(`incident_diagnosis.py:149`,无条件)→ 但这写的是 **Hermes 进程自己的 incident_store**(若有);
  2. `_writeback_diagnosis_artifacts`(`service_main.py:140`,无条件)→ HTTP 到 Gateway `/diagnosis/writeback` → `apply_diagnosis_writeback`(`diagnosis_writeback.py:65`)→ `record_incident_diagnosis`(`:76`)写 **Gateway 的** `incidents.diagnosis_json`。
- writeback 校验只要求 `diagnosis` 是对象、`confidence` 是 dict、`markdown` 是 str(`diagnosis_writeback.py:56-61`),而 `build_diagnosis` 在 failed/零 evidence 时仍产出这些字段,所以**即使 status=failed,Gateway 仍应收到 diagnosis_json**(除非 writeback 自身因网络/`WRITEBACK_SECRET` 缺失而失败,`service_main.py:240-242,263-270`)。

**因此 journal 的「incident_analysis 未写」最可能指的是 `incident_analysis` 表本身没有行**——这是 Hermes 路径的固有行为(它写 `incidents.diagnosis_json` 而非 `incident_analysis` 表),不是空后端导致的。需要和提观测的人确认:他们期望看到的 “analysis” 是 Console/voice 消费的 `incident_analysis` 表(`hooks/voice_context.py:152-163` 读它),还是 `incidents.diagnosis_json`。

### 4. 结论:实际路径与 A-3 最小正确改动点

**本次 smoke 实际路径**:alertmanager → Gateway ingress(建 incident)→ HTTP handoff → **Hermes `/diagnosis/sessions` → `run_diagnosis_session`(`toolsets/incident_diagnosis.py`)**。Legacy `hooks/alert_webhook.py` 全程未参与。

**A-3 最小正确改动点**:落在 **`toolsets/incident_diagnosis.py`**,核心是 `_derive_session_status`(`:591-603`)+ `_is_hard_failure`(`:584-588`)/ `TERMINAL_FAILURE_CODES`(`:28`)。要让「后端不可用/为空时不一票否决成 failed」:
- 不再让任意一路 `hard_failure` 直接 `return "failed"`;改为综合判断:若仍有任何成功 evidence → 至少 `partial`;若完全无 evidence → `needs_human`;仅在确需表达“整条诊断不可信”时保留 failed(或彻底改用 needs_human)。
- 这样 logs 那路(即便空)或任何一路存活就能让 session 落到 partial/needs_human,从而 writeback 携带一个非 failed 状态,下游不再当作硬失败。

**关于「仍写 analysis」**:
- 若指 `incidents.diagnosis_json`(Hermes 诊断产物):`_persist_diagnosis`(`:149`)与 writeback(`service_main.py:140`)**已无条件执行**,无需改动;真正要确认的是 writeback 是否因 `AIOPS_GATEWAY_URL`/`WRITEBACK_SECRET` 在 smoke 环境缺失而 `status=failed`(`service_main.py:238-242`)——这是另一类“没写成”的可能根因,需查 smoke 环境变量。
- 若指 `incident_analysis` 表(Console/voice 消费):则需要在 **Hermes 编排侧(`hermes/service_main.py` 或 `run_diagnosis_session`)新增一次 `upsert_analysis` 写入**(复用 `incident_store.upsert_analysis` `:543`),把 diagnosis 的 symptoms/suspected_root_causes/supporting_evidence/missing_evidence/next_best_actions 映射进表。这属于**新增写入点**,不是状态容错本身,范围更大,必须先与调用方确认指代。

## Caveats / Not Found

- 未直接读取 smoke 环境变量(`AIOPS_*_MCP_URL`、`AIOPS_GATEWAY_URL`、`AIOPS_HERMES_WRITEBACK_SECRET`)的实际取值;`_failed_tool_envelope` 命中 `backend_unavailable` 与 writeback 是否成功都依赖这些值,需在 deploy overlay / 集群 env 中核对(`deploy/k8s/overlays/dev-external` 相关,commit `984d66b`)。
- 无法从代码确认 journal 中 “incident_analysis 未写” 到底查的是哪张表/哪个 API 响应字段——存在 `incident_analysis` 表 vs `incidents.diagnosis_json` 的歧义,这是 A-3 改动落点(状态容错 vs 新增写入点)的关键分叉,建议立即与调用方对齐。
- `enqueue_diagnosis_session` 是异步后台线程(`service_main.py:175-176`),Gateway handoff 拿到的是 `202 queued`,最终 status 要从 Hermes session 缓存 / writeback timeline 事件读取,smoke 若过早读取可能看到 `queued` 而非最终态。
