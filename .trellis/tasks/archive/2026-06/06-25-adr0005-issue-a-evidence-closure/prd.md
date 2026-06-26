# ADR-0005 Issue A end-to-end evidence closure (parent)

## Goal

让「告警 → Gateway webhook → Hermes 诊断」链路在真实 dev-external 后端下跑出**可用的端到端结果**:
四路 evidence 该有数据时有数据、session 状态语义正确、诊断产物落库。这是 ADR-0005 Issue A 的
端到端验收锚点,在 06-24-deploy-monitor-switch-aiops-external 任务完成部署/切换后暴露出三个
独立的 spec 缺口,本 parent 统筹三个 child 各自闭环,parent 负责跨 child 的端到端绿灯。

## Confirmed Facts(已双向验证:研究子agent + 主 agent 读码)

触发链路(非 journal 最初假设):
```
POST /webhooks/alertmanager (apps/aiops_k8s_gateway/main.py:615)
  → alertmanager_webhook.py:320 handle_http_request → process_payload(建 incident)
  → trigger_hermes_diagnosis_session(HTTP 派给 Hermes service)
  → hermes/service_main.py:133 run_diagnosis_session (toolsets/incident_diagnosis.py:77)
```
Gateway 自身不诊断;legacy `hooks/alert_webhook.py` 全程未参与。

本次 smoke 观测到的「session failed / 四路只剩 logs / line_count:0」根因拆成三个独立缺口:

1. **应用不发 stdout**(child: aiops-stdout-logging):6 个 AIOps 核心 pod `kubectl logs` 全空,
   alloy 采不到 → Loki `namespace=aiops-dev` 无业务日志 → logs evidence line_count:0。
2. **无 aiops-dev ServiceMonitor**(child: aiops-dev-servicemonitor):kube-prometheus-stack 默认
   不 scrape 业务 ns → Prometheus `up` 22 条全是系统组件 → metrics 路无数据。
3. **单路后端不可达一票否决成 failed**(child: diagnosis-empty-backend-tolerance):
   metrics/k8s_read/topology 后端连不上 → envelope `error_code=backend_unavailable` →
   `_is_hard_failure` 命中 → `_derive_session_status:597` `if hard_failure: return "failed"` 一票否决,
   即使其他路有 evidence 也整体判 failed。

**对 journal 最初前提的两处校正**(已写入 child research):
- Hermes 路径 `_persist_diagnosis`(incident_diagnosis.py:149)**无条件**写 `incidents.diagnosis_json`,
  「failed 不写诊断产物」不成立。
- `incident_analysis` 表只被 legacy `hooks/alert_webhook.py` 写,与 Hermes 路径无关;journal 说的
  「incident_analysis 未写」是查错对象,Hermes 路径本就不写此表。故 child 3 范围收敛为「只修状态语义」。

## Child Tasks(独立可验收,无强依赖;若需顺序写在各自 prd)

| Child | 缺口 | 改动面 | 验收信号 |
|---|---|---|---|
| 06-25-diagnosis-empty-backend-tolerance | 状态一票否决 | `toolsets/incident_diagnosis.py` 纯代码 | 单路 backend_unavailable + 其他路有 evidence → session=partial,不再 failed |
| 06-25-aiops-stdout-logging | 应用不发 stdout | AIOps 服务日志配置 + 部署 | `kubectl logs` 有 lifecycle 行;Loki `{namespace="aiops-dev"}` 查得到业务日志 |
| 06-25-aiops-dev-servicemonitor | 无业务 metrics | monitor/deploy YAML | Prometheus `up{namespace="aiops-dev"}` 含业务 target |

执行顺序建议:child 3(纯代码、最快、不碰集群)→ child 1/child 2(数据面,需重新部署验证)。
三者无代码层强依赖,但 parent 端到端验收要三者都到位。

## Parent Acceptance Criteria(跨 child 端到端)

- [ ] 三个 child 各自 AC 满足并归档。
- [ ] 重跑 README 文档化 smoke(集群内 `POST /webhooks/alertmanager`,namespace=aiops-dev)后:
  - [ ] session status 非 `failed`(应为 `diagnosed`/`partial`,视后端数据完整度)。
  - [ ] `incident_evidence` 四路中 logs 与 metrics 至少各有非空 payload(stdout + ServiceMonitor 生效后)。
  - [ ] `incidents.diagnosis_json` 有诊断产物且脱敏生效。
- [ ] 端到端结论写回 ADR-0005 Issue A 锚点 / spec。

## Out of Scope

- 不写 LLM 诊断大脑(ADR-0003 后续)。
- 不接真实 Alertmanager 路由 + webhook HMAC(后续独立任务;smoke 仍用手工 POST)。
- 不补 middleware(emqx/redis/nacos…)ServiceMonitor。
- 不动 Console/Feishu 下游消费方(除非 child 引入新 session status 值,届时在 child 内评估)。
