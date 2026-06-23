# ADR-0004: 成本计量与预算守卫——采集随 0003 落地,展示后置,只存 token

Date: 2026-06-17

Status: Accepted

## Context

产品上希望 Console 展示诊断成本:某个任务(incident/session)消耗多少、消耗排行榜、按日/周/月聚合,并能"防爆表"。`docs/aiops-console-v1-contract.md`(AIO-87)已为此预留了 `/api/costs/summary`、`/api/costs/breakdown`、`/api/incidents/{id}/costs`、`/api/sessions/{id}/costs` 端点和 `group_by=team,service,incident,session,tool,model,day` 的契约形状。

但盘查代码后,成本链条的源头并不存在:

- Gateway 与 Hermes 中没有任何 token usage / cost 字段(`grep usage|input_token|output_token|cost` 全空)。`/api/costs/*` 端点未实现。
- 成本的唯一来源是 LLM 调用返回的 `usage`,而 ADR-0003 决定的"薄 LLM 编排"大脑**尚未实现**——`hermes/service_main.py` 只有四个证据 adapter(metrics/logs/k8s/topology)及其 synthetic 兜底,没有 LLM 调用层。
- 因此成本采集**无法独立于 0003 启动**:没有真实 LLM 调用就没有 usage。现在单独建成本后端,只能产出空壳或假数据,而假数据与 0003 反对 synthetic 证据是同一个病。

挂载维度是齐的:`incident_id` + `session_id` 这对 ID 已贯穿 webhook → handoff → writeback 全链路,成本只需作为新字段挂上去。

本 ADR 收敛成本计量与预算守卫的范围、存储模型、单位与触发方式。进程边界不变(浏览器只到 Gateway,见 ADR-0002);诊断大脑形态不变(见 ADR-0003)。

## Decision

### 1. 采集随 0003 落地,展示后置

成本**采集**与 0003 薄编排同时做:LLM SDK 返回的 `usage` 随 `/diagnosis/writeback` 挂到 `session_id`。成本**展示前端**(Console `/costs` 切片)后置,不进当前轮次。排序为:

`0003 薄编排 + usage 采集 + 单次硬上限` → `审批中心(后端已就绪)` → `状态查看(路由改造 + envelope 适配)` → `成本聚合 + 软告警` → `成本展示前端`

理由:采集和 0003 是同一件事,绑定;其余按后端就绪度排,不让从零的成本展示阻塞已就绪的审批。

### 2. 只存原始事实,美元算出来

每条 session 成本记录只落**不会变的物理事实**:`model`、`input_tokens`、`output_tokens`、`tool_calls`、时间戳。**不落美元。**

美元在查询时换算:`input_tokens × price[model].input + output_tokens × price[model].output`。价目表是一个无生效日期的 `{model: (input_price, output_price)}` dict,手工维护(厂商定价表),与 0003 的 `base_url`/`api_key`/`model` 配置同处,沿用 `AIOPS_*` 环境变量/配置模式。

历史成本**用当前价覆盖全历史**,不做"按当时价冻结"。理由:只要 token + model 原样存死,"按当时价冻结"是任何时候都能补的纯展示层功能(加一张带 `effective_from` 的价目表 + 时间点匹配),零数据迁移。现在建冻结是为推测需求建抽象(违背 0003 同一纪律),而日常看的是近 7/30 天,定价多数未变,这层会白建。

### 3. token 与美元双轨展示

Console 前端 token / 美元可切换——同一份数据两种渲染。`/api/costs/*` 响应同时给 token 字段(契约 summary 里已有 `input_tokens`/`output_tokens`)和换算出的 `estimated_cost_usd`。

### 4. 防爆表 = 分级双闸

- **单次硬上限(token)**:发生在 0003 编排循环内,SDK 当场返回 usage,直接判断 `if session_tokens > AIOPS_HERMES_MAX_SESSION_TOKENS: break`,产出"因成本上限中断"的部分诊断。这是熔断失控工具调用循环的唯一硬手段,用 token 避免热路径换算。
- **日/月软告警(美元)**:跨阈值只记 audit + 发通知,**不打断诊断**——月底硬拒诊断生产故障是灾难。阈值是**全局单一**美元值,一个环境变量 `AIOPS_MONTHLY_COST_ALERT_USD`,不做 per-team 预算(无数据时的提前优化,真有 team 月月超标再加)。

### 5. 软告警用写时检查触发,不引入调度

日/月累计在 `/diagnosis/writeback` 落 usage 的同一事务里跑一句 `SELECT SUM(tokens) WHERE month=本月`,换算美元跨阈值即调 `notification_center.send_notification(...)` 发飞书。零新进程、零调度依赖、告警实时。加一个"本月已告警"标记位防刷屏(一个 bool,不建告警去重服务)。

## 非目标

- 不在 V1 落美元到库;不做按当时价冻结(token+model 存死,日后纯展示层可补)。
- 不做 per-team / per-service 预算阈值(V1 全局单一线)。
- 不做完整云 FinOps;成本仅覆盖 AIOps 诊断操作(LLM + 工具调用)。
- 不引入定价服务、预算配置表、告警去重服务、cron/调度框架。
- 不把成本展示前端纳入当前轮次。

## Consequences

Positive:

- 成本数据从 0003 第一天起就在库,日后做聚合/排行榜/日周月无需回头补采集。
- 只存原始事实,厂商改价不会让历史数据出错或需迁移。
- 排行榜、日周月、任务消耗都是数据齐后的纯查询,契约 `group_by` 已覆盖。
- 防爆表实时生效(写时检查),不依赖有人打开页面,半夜也告警。
- 单次硬熔断挡住失控工具循环,软告警挡累积超支,互不误伤诊断质量。

Costs:

- 成本采集与 0003 强绑定,0003 不落地则成本无从谈起。
- 价目表手工维护,换 model 需同步更新(配置注释标明)。
- 每次 writeback 多一次月度 SUM 聚合查询(量小,可接受)。
- 美元为查询时估算,非权威账单;缺失记录渲染为 unavailable,不渲染为 0。

## 隐含前置(非本 ADR 决定,但阻塞落地)

- **ADR-0003 尚未实现**:`hermes/` 只有 adapter,无 LLM 编排层。成本采集的起点是 0003,不是 Console。
- **Gateway 路由是裸 `http.server` 精确匹配,无路径参数解析**:契约里 `/api/incidents/{id}/*`、`/api/costs/*` 等带路径参数的端点要落地,须先改造路由机制。这是状态/成本前端的隐藏前置。
- **爆表"控制"动作活在 Hermes 编排层,不在 Console**:Console 只展示阈值与告警历史,真正的熔断 `break` 在 0003 工具循环里,排期时勿当前端功能。

## Alternatives Considered

**成本展示也进当前轮次:** 数据源(0003)不存在,只能做空壳或假数据,与 0003 反 synthetic 纪律冲突。

**美元落库 / 按当时价冻结:** 厂商改价后历史数据出错且难迁;冻结需带日期价目表 + 时间点匹配,是为推测需求(历史定价复盘)提前建抽象,而 token+model 存死后该能力随时可纯展示层补齐,零迁移。

**per-team 预算阈值:** 在尚无一条成本数据的系统上做精细化治理,典型提前优化。先用全局线验证采集+告警链路,真有 team 超标再加。

**软告警用 cron/定时任务或读时检查:** cron 引入调度依赖与新进程;读时检查在无人看页面时失效,等于不防。写时检查蹭已有 writeback 入口,零新进程且实时。

**硬熔断用于日/月总额:** 月底拒绝诊断生产故障是灾难。硬熔断只用于单次上限挡失控循环,总额用软告警。
