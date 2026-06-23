# ADR-0005: 诊断可观测性——业务事实自建、诊断过程结构化先行,Langfuse 后置不内嵌

Date: 2026-06-17

Status: Accepted

## Context

产品上希望「不接受黑盒」:能看到诊断 AI 在想什么、做什么——prompt(含动态生成)、tool call、完整调用链路、token/成本/延迟。盘查代码后现状如下:

- `hermes/` 与 `incident_diagnosis.py` 中**没有任何 LLM 调用**(`grep chat.completions|messages=|tool_use|usage|system_prompt` 全空)。`incident_diagnosis.py` 仍是 ADR-0003 要替换的关键词规则引擎。**没有 agent,就没有 prompt / LLM tool-use / token 可观测。**
- 关联骨架已齐:`incident_id + session_id` 贯穿 webhook → handoff → writeback(见 ADR-0004)。
- `audit_log` 表已落执行类 tool 调用(`tool_name / request_id / incident_id / result / scope / actor / role`)——但这是「Connector 执行了什么命令」的合规审计,不是「LLM 决定调哪个工具、传了什么参数、看到什么观测」的决策链。
- `cost_records` 表已建(`model / input_tokens / output_tokens / estimated_cost / timestamp`),但采集源是 LLM 的 `usage`,LLM 层不存在,故表为空(ADR-0004 已写明「成本采集无法独立于 0003 启动」)。

结论:四项可观测诉求的数据源全部卡在 ADR-0003 的 LLM 编排层。本 ADR 收敛**可观测性的分层、埋点位置、存储归属与第三方 trace 工具(Langfuse)的引入边界**。进程边界不变(浏览器只到 Gateway,见 ADR-0002);诊断大脑形态不变(见 ADR-0003);成本存储模型不变(见 ADR-0004)。

## Decision

### 1. 按消费者分两层,不混为一谈

| 层 | 内容 | 生命周期 | 消费者 | 存储 |
| --- | --- | --- | --- | --- |
| **业务事实** | token、成本、tool 执行结果、审批审计 | 永久 | Console / 审计 / 财务 | 自建 SQLite(`cost_records` / `audit_log`,已有) |
| **诊断过程** | prompt 全文、每步 span、延迟、模型每步所见 | 天/周 | 开发调大脑 | 自建 `diagnosis_trace` 表,后置可接 Langfuse |

业务事实必须自主可控(合规、可聚合、可 code review),不依赖可被关闭的外部 SaaS。诊断过程是调试数据,可视化价值高但非合规要求。

### 2. 大脑落地第一天就把过程数据按 trace 结构存死

ADR-0003 薄编排实现时,同步埋点:每步工具调用落 `diagnosis_trace`(`session_id`、`step_index`、`tool_name`、`tool_args`、`observation_ref`、`duration_ms`、`model`、`input_tokens`、`output_tokens`),prompt 全文落快照(文件或 `prompt_snapshot` 字段)。`cost_records` 增加 `latency_ms` 列。

延续 ADR-0004「采集先落、展示后置」:**埋点位置定对,数据就在库里;消费先用 JSON / 极简 Console 视图,不自建 trace 瀑布图。**

### 3. Langfuse 后置,做引擎不做内嵌

调大脑真觉得痛(prompt 调试/链路回放频繁)时,引入 Langfuse:LLM 编排处加一层 SDK exporter,把已结构化的 prompt/tool/token/latency 上报。因为数据已按 trace 结构存死,**接 Langfuse 只是加 exporter,不回头补数据。**

Langfuse **不替代** `audit_log` / `cost_records`——审批审计与成本是合规事实,自建表是权威源,Langfuse 只是过程数据的可视化镜像。

### 4. UI 先跳转,不 iframe 内嵌

引入 Langfuse 后,Console **不 iframe 内嵌**其 UI。理由:iframe 内嵌使浏览器直连 Langfuse,破 ADR-0002「浏览器只到 Gateway」这条核心边界;且 Langfuse 是独立整站(自有登录/权限/CSP),内嵌会撞二次登录、X-Frame-Options、权限错位。

替代:Console 每个 incident 放「查看诊断链路」按钮,**带签名跳转**到对应 Langfuse trace(用户主动开新页,非页面内直连)。普通用户看 Console 自渲染的关键字段,深度调试跳 Langfuse 用全功能 UI。

若「跳转太烦、要无缝」成为实痛点,再考虑 **Gateway 反代 Langfuse**(`/observability/*` 反代 + auth 注入,保持浏览器只连 Gateway)——那是确认体验痛后的第二步,不提前做。

## 非目标

- 不在 ADR-0003 落地前建 prompt / trace / token 采集(无 LLM 即无数据,同 ADR-0004 纪律)。
- 不自建 trace 瀑布图 / prompt diff / token 火焰图前端(这是引入 Langfuse 的理由,不是自建的理由)。
- 不让 Langfuse 替代审计或成本权威源。
- 不 iframe 内嵌 Langfuse UI;不提前做 Gateway 反代。
- 不把诊断 prompt(含集群拓扑/日志片段)发往 Langfuse SaaS——若引入,自托管,数据不出境。

## Consequences

Positive:

- 业务事实与诊断过程各归其位,选型不再纠结「自建 vs 工具」。
- 埋点随大脑第一天落地,过程数据在库,日后接 Langfuse 零数据回补。
- 不破「浏览器只到 Gateway」,不提前引入新进程与数据出境。
- 审计/成本权威源始终自主可控,不绑外部 SaaS 生命周期。

Costs:

- ADR-0003 不落地则过程数据无从采集(同成本,强绑大脑)。
- Langfuse 引入前,prompt/trace 消费是极简视图,调试体验不如现成工具——这是刻意的延迟决策,换不提前引依赖。
- `diagnosis_trace` 的 prompt 快照可能含敏感证据片段,存储需与 `k8s_redact` 同等脱敏对待。

## 隐含前置

- **ADR-0003 尚未实现**:埋点的宿主是薄编排层,它是所有过程数据的起点。
- **主线 Hermes adapter 当前不持久化证据**:`incident_diagnosis.run_diagnosis_session` 收到的全量 observation 仅以 ref 流向 diagnosis,原始证据在函数返回后丢弃,主线从不调 `incident_store.add_evidence`(`hooks/alert_webhook.py` 的 legacy 路径才调)。评测集采集与 trace 埋点共用这个尚不存在的切入点。
- **脱敏关卡在出境,不在入库**:入自己受控库(`incident_evidence` / `diagnosis_trace`)用现有 `redact_k8s_output` / `redact_sensitive_text` 兜底先落;真正的通用文本脱敏卡在**出境那一刻**(prompt 发外部 provider、trace 发 Langfuse),与下文 provider 出境一致性合并为一个关卡。

## 落地路径

本节由一次设计逼问(grilling)收敛,记录从「证据采集 + 真根因回填 + 内外部 LLM 可切」到可独立交付 issue 的决策与拆分。

### 钉死的决策

1. **采集切入点归属——顺势直连。** 在 `incident_diagnosis.py` observation 产生处直接调 `add_evidence`,顺着 Hermes 现有的直连写库(timeline / diagnosis persist 已如此)。「Hermes 不该碰持久层」是独立的边界债,单独立 issue 收口,不绑在采集这件急事上。
2. **落库范围——成功 + 缺口都落,failed 不落。** `succeeded` 存全量 payload;`partial` 存部分 payload、低 confidence;`skipped` 存空 payload、summary 记 reason;`failed`(adapter 抛错)只走 audit,不进 evidence 表。理由:评测集要复现「大脑当时看到的完整现场」,缺口本身是判断约束;但代码故障不该污染证据。
3. **脱敏时机——入库用现有兜底,脱敏卡在出境。** 入自己受控库照常先落;通用文本脱敏定位为出境前关卡,与 provider 出境一致性合并。日志原文落自己受控库可接受,真正的风险在出境。
4. **真根因回填——API-first,绕开路由改造。** Gateway `POST /api/case-profile`(incident_id 在 body),绕开 ADR-0004 点明的路径参数路由改造前置;body 含 `final_root_cause + root_cause_category + key_evidence_refs + effective_actions`。`root_cause_category` 供回放按类目带容差打分,不靠字符串相等。Console 表单后置。
5. **内外部 LLM 出境一致性——软约定 + 启动日志。** provider 仍是 `base_url + api_key + model`(ADR-0003),默认指内部端点(configmap 已做);启动打印 base_url 与出境提示日志,使出境可见可审计。不建内网白名单硬拦(易错易绕、过度防御);真要拦,拦在出境前的脱敏关卡。
6. **采集与大脑时序——采集排在 ADR-0003 之前。** 现在就把采集针扎进当前关键词引擎,打破「大脑验收要评测集、评测集要等大脑落地才开始采」的死锁;ADR-0003 重写 `incident_diagnosis.py` 时顺手把采集针迁到 LLM 工具回调处(本就要动该文件)。

### 可独立交付的 issue

| Issue | 内容 | 前置 |
| --- | --- | --- |
| **A 证据采集落库** | observation 产生处调 `add_evidence` 存全量 payload,按决策 2 分类落库,按决策 3 脱敏 | 无 |
| **B 真根因回填端点** | Gateway `POST /api/case-profile` → `upsert_case_profile`(现成),含 `root_cause_category` | 无 |
| **C dev-external 配置坑** | `PROMETHEUS_URL`/`LOKI_URL` 注释指向实际有数据的后端;`AIOPS_NAMESPACE_SCOPE` 放开到要诊断的 namespace | 无 |
| **D 出境一致性约束** | provider 启动打印 base_url + 出境日志;通用文本脱敏作为出境前关卡 | 写进 ADR-0003 大脑 issue 的约束 |
| **E trace 埋点** | 与采集共切入点;`diagnosis_trace` 表 + `cost_records.latency_ms` | ADR-0003 大脑落地 |
| **F 进程边界收口(债)** | Hermes 直连 `incident_store`(timeline + persist + evidence)收进 Gateway writeback | 独立,可延后 |

依赖链:A、B、C 现在并行(均无前置、不依赖 LLM 大脑)→ 真故障自然沉淀评测集 → ADR-0003 大脑 + E 埋点 → 回放验收(按 `root_cause_category` 打分)。D 随大脑,F 随时。

### 验收锚点

- A:`overlays/dev-external` 接真后端跑一次告警,确认四路证据落 `incident_evidence`、格式可回放、脱敏生效。
- B:curl 回填一个 incident,`get_case_profile` 读回一致。

## Alternatives Considered

**一开始就引入 Langfuse 吃现成 UI:** prompt/trace 可视化体验碾压自建,但带来新进程 + 数据出境风险,与「浏览器只到 Gateway、数据边界收紧」冲突;且替代不了必须自主可控的审计/成本源。在调大脑频率被验证为高之前,是为可视化提前引依赖。

**全自建(含 trace 瀑布前端):** 零新依赖、数据主权在手,但自写 trace 可视化成本高、体验差,正是 Langfuse 开箱即用之处。故业务事实自建、过程可视化留给后置工具。

**iframe 内嵌 Langfuse:** 看似省事,实则破浏览器边界、撞二次登录与 CSP、权限错位。无缝体验应由 Gateway 反代实现,且仅在跳转被验证为痛点后做。
