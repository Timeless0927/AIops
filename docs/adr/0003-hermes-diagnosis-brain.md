# ADR-0003: Hermes 诊断大脑选型——薄 LLM 编排,不引入自治 agent 框架

Date: 2026-06-17

Status: Accepted

## Context

`hermes/` 进程边界已经存在,但其诊断逻辑(`toolsets/incident_diagnosis.py`)是一个规则引擎 skeleton:用关键词匹配告警文本选取固定的工具采集计划,再用关键词从硬编码字符串里选 root cause,confidence 是手搓公式。adapter 在未配置 MCP URL 时返回 synthetic 假证据。它能跑通端到端 smoke,但产出的是模板拼接,不是诊断。

仓库里同时存在一个未初始化的 submodule `hermes-agent`,指向 `github.com/NousResearch/hermes-agent`(本地装有 v0.10.0),定位是 "self-improving AI agent — creates skills from experience, improves them during use"。这是项目早期为"自我学习的诊断大脑"引入的选型,但在 split-service 重构后被晾置,CLAUDE.md 已将其标为"非主线"。

结果是仓库里物理地存在两条未合并的路线:一个空的自治 agent submodule,和一个手搓的规则引擎壳。这是"架构纠结"的根源。本 ADR 收敛诊断大脑的路线。

进程边界(Gateway / Hermes / Connector / MCP / Console)不在本 ADR 讨论范围内,见 ADR-0002,结论是边界正确、保留。本 ADR 只决定 `hermes/` 边界内部那颗大脑怎么做。

> 命名说明:本仓库的 `hermes/`(及 `AIOPS_HERMES_*` 环境变量、`"service": "hermes"` 等)指**自研诊断服务边界**,与本 ADR 决定删除的 NousResearch `hermes-agent` 外部项目**无关**。两者重名是早期选型的历史遗留,删除 submodule 后该重名冲突消失。改名见 Future Work。

## Decision

诊断大脑采用**薄 LLM tool-use 编排**,替换 `incident_diagnosis.py` 的关键词匹配:

1. **底座——LLM 现场推理**:把现有四个证据 adapter(`_metrics_adapter`、`_logs_adapter`、`_k8s_read_adapter`、`_topology_adapter`)注册为 LLM 工具,模型现场读证据、自主决定调用顺序、推理根因,输出结构化诊断。每个故障都从现场证据独立推理,这一步永不跳过。
2. **经验——案例记忆**:处理过的 incident(证据特征 + 确认根因 + 有效处置)沉淀到一张表,下次相似故障检索出来,作为**可选线索**拼进 prompt。它能加速、补盲、调置信度,但不能否决现场证据。沉淀与检索由本项目代码控制,不交给会自我改写的 agent。
3. **方法论——文本 runbook**:人预先编写的排查思路以 Markdown 形式存在 git 中,按告警类型检索后,作为"建议排查路径"拼进 system prompt。runbook 引导模型查什么、什么顺序、看哪些指标,但模型可结合现场证据偏离它,不是强制流程。
4. **基础设施交给 LLM SDK**:重试、超时、token 管理、tool-use 循环由厂商 SDK 兜底,本项目只写业务编排(注册工具、收集证据、组织诊断、writeback)。
5. **Provider 留接缝,不建抽象**:按 OpenAI-compatible 协议写调用层,`base_url` + `api_key` + `model` 三件套做成配置。换 provider = 换配置,不改代码。不引入跨厂商兼容层依赖(如 LiteLLM)。

执行边界不变:模型产出的任何 mutation 都是 action proposal,经 Gateway 的 RBAC / 审批 / 审计 / envelope 执行,绝不在 Hermes 进程内直接执行。

## 删除 hermes-agent submodule

删除 `hermes-agent` submodule 及 `.gitmodules` 中对应条目,从 `requirements.txt` 移除 `-e ./hermes-agent[cli,feishu,dingtalk]`。理由见 Alternatives。飞书/钉钉集成已由本项目 `hooks/` 和 `apps/aiops_k8s_gateway/notification_center.py` 自行实现,不依赖该 submodule。

## 验收标准

诊断质量靠**真实故障回放集**度量,不靠"看着像样"的直觉:人工固化至少 10 个真实历史 incident(告警 + 当时的 metrics/logs/k8s 现场证据 + 事后确认的真根因),让 Hermes 回放,对比诊断与真相的命中率。这是大脑从"玩具"毕业的硬门槛。V1 验收线是"省人力"(on-call 初步排查负担显著下降);"比人准"是北极星,不作为 V1 验收线。

### 落地状态(跨 child 结论回填,parent `06-29-adr0003-diagnosis-brain`)

清单为代码侧截止 2026-06-30 的落地结论,并补充 2026-07-01 真实回放运营收口结果;与上面「验收标准」对照区分**已可测的代码能力**与**真实故障回放度量**两个验收层。

- **大脑本体已替换**:三个 child(provider 调用层、`incident_diagnosis.py` tool-use 重写 + `diagnosis_trace`/`cost_records.latency_ms`、replay harness 代码侧)已并 archived,`run_diagnosis_session` 现为薄 LLM tool-use 编排,keyword 路径保留为 `provider=None` / `ProviderUnavailable` / final-JSON 解析失败时的降级,`COLLECTOR_VERSION` 标 `llm-tooluse-v1`。
- **合成证据删除**:旧 `_synthetic_*` adapter 已删,未配置 adapter 返回 partial envelope(兑现本 ADR 反 synthetic 纪律)。
- **cost 接缝**:cost 采集走模块级 `toolsets.cost_guard.record_cost`(ADR-0004 进程);为让单测与 parent 端到端验收可证 `latency_ms>0`,`_record_provider_cost` 增加 optional store 注入——store 提供 `record_cost` 时优先调用,否则 fallback 到模块级 `cost_guard`。生产默认 `incident_store` 无 `record_cost`,仍走 `cost_guard`,ADR-0004 path 不被绕过;注入接缝仅服务代码侧验收。
- **代码侧验收已闭环(parent AC #1–#4)**:三个 child 归档;FakeProvider 全链路 smoke(`test_parent_ac_full_chain_smoke_four_channels_trace_and_cost_latency`)证四路 evidence 落库 + `diagnosis_trace` ≥5 行 + `cost_records.latency_ms`>0;provider 不可达走 keyword 降级、状态机 `needs_human/partial/diagnosed` 0 回归;出境日志可见。ADR-0003 child scope `pytest` 76 passed(已知 pre-existing 的 `tools` submodule 缺依赖致 `test_cost_guard` 等 collection error,与本 ADR 无关,按 PRD 排除)。
- **真实回放 V1 硬门槛已收口(2026-07-01)**:在真 Kubernetes + 真 Prometheus/Loki + DeepSeek OpenAI-compatible provider 环境中沉淀 10 个 `synthetic:false` fixture,均完成 `incident_evidence` 现场证据、`diagnosis_trace` tool-use 轨迹、人工 `case-profile.root_cause_category` 回填与 fixture 导出。`python3 tests/replay_incident.py` 报告 `Real fixtures: 10`、`Synthetic fixtures: 2`、`Real hit-rate: 100.0%`;`--validate-taxonomy` 通过。本轮 10 条真实 fixture 聚焦同一类 `bad_release_deploy`/PodCrashLooping replay 场景,已达 V1「省人力」最小毕业线;跨类泛化与更多真实故障类型扩充进入后续持续运营,不再阻塞本 ADR V1。
- **真实运营暴露并修复的接缝**:live provider 跑出两类单测没覆盖的问题——`LLM_TOOLUSE_MAX_TURNS=6` 使真实工具链过早 fallback,现改为读取 `AIOPS_LLM_TOOLUSE_MAX_TURNS` / `AIOPS_AGENT_MAX_TURNS`(默认仍 6);真实模型 final answer 可能带 ```json fence 或前后文,现从 final content 提取首个平衡 JSON object 后解析,仍无 JSON 时降级。
- **改名(Future Work)未做**:`hermes/` 改名随大脑大改一并完成,本 parent 落地未触发,误导由"命名说明"与 CLAUDE.md 注解消解。

> 验收分层结论:**代码能力层(可测)已就绪并归档**;**真实故障命中率层已通过 ≥10 real fixture 回放收口**,本 ADR V1 毕业线达成。后续重点从"能否毕业"转为"跨更多故障类目扩充样本与持续校准"。

## 非目标

- 不微调诊断模型(样本量不足,且会过拟合旧故障)。
- 不做自动改写 prompt / 策略的自我改进(C 类)。
- 不做强化学习反馈闭环(D 类)。
- 不引入会自我创建/改写 skill 的自治 agent(即 NousResearch Hermes 的核心能力)。
- 不做可执行 skill(携带脚本、绑定工具、定义执行流程的单元)。排查思路只以文本 runbook 形式存在;执行能力只经 adapter / Gateway。
- 不建跨厂商 LLM provider 抽象;只留单点配置接缝(延续 CLAUDE.md "不做 Brain Provider 抽象")。

## Consequences

Positive:

- 命门(诊断大脑)从规则引擎升级为真正的现场推理,处理未见过的故障是底座能力而非回退。
- 每一步证据进入、模型所见、判断依据都可落 audit,满足可解释、可复现、可审计要求。
- 经验复用(案例记忆)和方法论(runbook)都是本项目掌控的数据,版本化、可 code review、不漂移。
- Provider 可按环境切换,不绑定单一厂商,不深绑外部 agent 框架。
- 仓库两条路线合并:壳保留并填入真大脑,自治 agent submodule 删除。

Costs:

- 需要人工攒并维护真实故障回放集,这是持续成本,但也是质量的唯一客观尺子。
- 需要人工编写并维护 runbook 知识库。
- 案例记忆的相似度检索质量需要随案例量增长调优。
- "薄编排"仍需本项目维护,不是零代码;但这部分是不可外包的业务逻辑。

## Future Work

- **Hermes 改名**:当前 `hermes/` 与已删除的 NousResearch `hermes-agent` 重名,易误导。删除 submodule 后重名冲突消失,但名字本身仍不够自解释。改名(如 `diagnosis` / `diagnosis-service`)涉及约 81 个文件、约 648 处引用,其中混有 `AIOPS_HERMES_*` 环境变量、`"service": "hermes"` 响应字段、部署 YAML service 名和跨进程 contract,是一次带回归风险的契约迁移。**不单独做**:在本 ADR 重写诊断大脑、本就要大改该服务并跑回归时一并完成。在此之前,误导由上文"命名说明"和 CLAUDE.md 注解消解。

## Alternatives Considered

**接回 NousResearch Hermes(初始化 submodule,用其 self-improving 大脑):**

- Benefit:agent loop、记忆、自学习现成。
- Cost:其核心卖点 self-improve(自创建/改写 skill、行为会漂移)正是本项目划掉的需求(见非目标 C/D),与"记忆只当可选线索、可审计、可复现"的底线系统性对撞;"锁定 self-improve / 受保护 skill"在上游仍是 open issue(#17583、#25083),非成品;default configuration 有公开的安全审计问题(#7826,4 Critical / 9 High);它是完整 agent runtime 而非一个诊断函数,塞进注入式 adapter 接口的适配层 + 加固成本很可能超过自写薄编排。最值钱的能力对本项目是负债。

**用 pi(earendil-works/pi)或其他 coding agent CLI 当底座:**

- Benefit:成熟 harness,免写 agent loop。
- Cost:品类错配。pi/Claude Code/Codex 是为"在仓库里读写代码"而生,世界观预设文件系统、代码、shell;诊断场景没有仓库、没有改文件,工具是 MCP adapter。把它掰成诊断 agent 的成本超过自写薄编排,且重蹈"引一个为别的场景而生的大物件再阉割"的覆辙。

**手搓一切(连 LLM HTTP 调用、tool_use 解析都自己写):**

- Benefit:零依赖。
- Cost:重复造 SDK 已经兜底的基础设施(重试、超时、token、工具循环),没必要。

**保留规则引擎现状:**

- Benefit:确定性、零 LLM 成本。
- Cost:无法读懂任意故障的证据,新故障类型只能回退到 "undifferentiated incident"。这正是"玩具感"的来源,与"省人力/比人准"目标冲突。
