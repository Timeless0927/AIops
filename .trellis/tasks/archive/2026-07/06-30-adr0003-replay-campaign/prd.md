# ADR-0003 ≥10 真实故障回放运营(ADR-0005 A/B 闭环 + live 冻结)

> Parent: `archive/2026-06/06-29-adr0003-diagnosis-brain`(已归档,parent AC#1–#5 代码侧闭环,本运营 task 直达 ADR-0003 §验收标准 V1 硬门槛)。
> Sibling: replay harness 代码侧(child-3,`tests/replay_incident.py`,frozen-fixture 回放 + tolerance matrix)已就绪。

## Goal

在**真 Kubernetes + 真 Prometheus/Loki 后端(loki ns)+ 真 LLM provider** 的环境里,
逐一在新建业务 ns 上制造真实故障 → 触真告警 → 真后端采证 + **真 LLM tool-use 推理** →
人工回填真根因(`root_cause_category`)→ 把每个跑通的 incident 的真现场证据 + 真根因
**冻结成 replay harness 可消费的 fixture**。沉淀 **≥10 个真实 fixture**,跑 harness 回放,
命中率面对 ADR-0003 §V1 验收线「省人力」。

这是 ADR-0003 大脑从「玩具」毕业的硬门槛;代码侧(parent `06-29-adr0003-diagnosis-brain` AC#1–#5)已
全部就绪,本 task 的全部阻塞都是运营动作 + 一个轻量 fixture 导出代码件,无产线行为变更。

## Why now

- ADR-0003 §验收标准:「人工固化至少 10 个真实历史 incident … 回放对比命中率,这是大脑毕业的硬门槛,
  V1 验收线是省人力」。代码侧落地状态节已把这层明确挂为「运营债」。
- ADR-0005 §落地路径 Issue A/B(真采证 + 真根因回填)在本 repo 已就位(代码件 archived),
  本 task 跑通二者并真正采到数据。
- replay harness(child-3)已就绪但只有 2 个 synthetic fixture(synthetic 永不计入 ≥10 真实门槛);
  需本 task 喂入真实冻结数据。

## Confirmed Facts(环境接缝已核)

- **真后端在 loki ns**:`dev-external` overlay 的 `PROMETHEUS_URL=http://prometheus-stack-kube-prom-prometheus.loki.svc.cluster.local:9090`、
  `LOKI_URL=http://loki.loki.svc.cluster.local:3100`(loki ns 的栈)。
- **真 LLM provider 接缝在**:`configmap:21-23` `AIOPS_MODEL_PROVIDER=custom` / `AIOPS_MODEL_NAME=gpt-5.4` /
  `AIOPS_MODEL_BASE_URL=http://model-service.default.svc.cluster.local/v1` + `secret:AIOPS_MODEL_API_KEY`。
  自写 OpenAI-compatible tool-use 循环(child-1 provider 调用层),不引 SDK。
  ⚠️ **前置待人工确认**:你的真 model 端点是否就是 `model-service.default` / `gpt-5.4`,
  且真支持 tool-use;否则在 overlay/secret 改 `AIOPS_MODEL_*` 三件套(prd 不动代码,只动配置)。
- **诊断目标 ns 缺位**:`AIOPS_NAMESPACE_SCOPE=aiops-dev`(平台自身,通常非诊断目标)。
  本 task 运营**新建一个业务测试 ns**(起测试 pod 制造真故障),overlay 的
  `AIOPS_NAMESPACE_SCOPE` 放开到该 ns。Connector RBAC + Connector 采集只到该 ns。
- **真采证针已接(ADR-0005 Issue A)**:`incident_diagnosis.py` observation 产生处调
  `incident_store.add_evidence` 四路(metrics/logs/topology/k8s)落 `incident_evidence` 表,
  脱敏生效。
- **真根因回填端点已就绪(ADR-0005 Issue B)**:Gateway `POST /api/case-profile`(body 含
  `incident_id`+`final_root_cause`+`root_cause_category`+`key_evidence_refs`+`effective_actions`,
  绕开路径参数路由)→ `incident_store.upsert_case_profile`;`get_case_profile` 读回一致。
- **incident_evidence 可读**:`incident_store.list_evidence(incident_id)` 返回四路 payload,
  fixture 导出器靠它 + `get_incident` + 回填的 case_profile 组装 fixture,**不碰产线代码**。
- **harness 只支持 frozen-fixture 回放**:`tests/fixtures/incidents/<id>/incident.json` +
  `evidence/*.json` + `truth.json`,ScriptedProvider + FrozenAdapter 重放预测,按
  `root_cause_category` 经 tolerance matrix 打分。**当前无 live 采证→冻结 fixture 的导出路径**,
  本 task 需新增一个导出器(轻量,只读 store + 写 fixture 目录)。

## Scope

### In(本 task 做的事)

1. **新建业务测试 ns + 测试 pod 清单**:一个新建 ns(非 aiops-dev),起能真触告警的测试 pod
  (memory-pressure / cert-expiry / crashloop / pvc-full / node-not-ready 等,sibling spec
  `skills/sre/runbooks/{...}` 已有对应 runbook 可参考故障形态)。
2. **改动 dev-external overlay**:放开 `AIOPS_NAMESPACE_SCOPE` 到新 ns;按需修 `AIOPS_MODEL_*`
  三件套指向真 provider;重 apply overlay。
3. **新增 fixture 导出器**(轻量代码件,只读 store + 写 `tests/fixtures/incidents/<id>/`):
   - 从 live 跑通的 incident 读 `incident_evidence`(四路 payload)+ `incidents` + 回填的
     `incident_case_profiles`(真根因 + `root_cause_category`)组装成 harness 需要的
     `incident.json` / `evidence/<source>.json` / `truth.json`。
   - 不改 `incident_diagnosis.py` / `run_diagnosis_session` / 产线行为。
   - 与 harness 自带的 `synthetic: true` 区分:导出器落 `synthetic: false`(或省略该标记)。
4. **运营闭环 ×≥10**:逐个制造真故障 → 真告警 → 真后端采证 + 真 LLM 推理 → 人工确认/修正真根因 →
   `POST /api/case-profile` 回填 → 导出器冻结成 fixture → 入 `tests/fixtures/incidents/`。
5. **跑 `tests/replay_incident.py --sweep`**:10 个真实 fixture 回放,harness 报告 real 列命中率。
   不达标时调 tolerance matrix(`ROOT_CAUSE_CATEGORIES`/`CATEGORY_GROUPS`,跑
   `--validate-taxonomy` 自检无 dangling)或回查 provider/runbook。
6. **回填 ADR-0003**:把 ≥10 真实 fixture 命中率写回 ADR-0003 §验收标准「落地状态」节替换
   「运营债」那行,作为 V1 验收正式收口。

### Out of Scope

- 改产线诊断大脑(`incident_diagnosis.py` / `run_diagnosis_session` / provider 调用层)——已就绪不动。
- 微调模型 / self-improve / 强化学习闭环(ADR-0003 非目标)。
- ADR-0004 成本展示前端、ADR-0005 Langfuse 后置、Hermes 改名(均刻意延后)。
- ≥10 之外的 fixture 规模扩张(V1 验收线只要 ≥10;泛化后置)。

## Acceptance Criteria

- [ ] 新业务 ns 内能真触告警,后端真采到证据(四路任一空均记原因,不空跑)。
- [ ] 真 LLM provider 连通,`run_diagnosis_session` 走 `llm-tooluse-v1`(非 keyword 降级),
      diagnosis_trace / cost_records latency 落库。
- [ ] ≥10 个真实 incident 各完成:采证四路落 `incident_evidence` + 回填 `case-profile` 含
      `root_cause_category` + 导出器冻结成 fixture(`synthetic:false`,harness 可消费)。
- [ ] `python tests/replay_incident.py --sweep` 报告 real_count ≥10,real 列命中率可读(非 0/0)。
- [ ] tolerance matrix 每个新真根因类目可回收;`--validate-taxonomy` 无 dangling。
- [ ] 结论写回 ADR-0003 §验收标准「落地状态」节,标注 V1 验收线达成与否。
- [ ] fixture 导出器有单测(读 fixture 回放一致);harness 自身不回归(76 passed 子集仍绿)。

## Risk / 前置确认(动手前解决)

- **真 LLM provider 端点确认**:你的真 provider 是否 `model-service.default/gpt-5.4`?
  是否支持 tool-use?不支持则 provider 调用层(child-1)无法走真 tool-use,V1 命中率失真。
- **诊断目标 ns 真有故障可触**:新 ns 测试 pod 要真能让 Prometheus 抓到指标 + Loki 抓到日志
  (即 ADR-0005 Issue 当时 stdout 采集 + ServiceMonitor 对新 ns 生效;若新 ns 未被 scrape,
  采证恒空——需顺带确 ServiceMonitor 选择器/Pod 注解覆盖新 ns)。
- **Connector RBAC 到新 ns**:`AIOPS_NAMESPACE_SCOPE` 放开后 Connector 的 k8s 证据采集 +
  RBAC 仅限该 ns(契约约束),需 dev-external 的 Connector RBAC 也覆盖新 ns。