# V1 功能测试矩阵

本矩阵覆盖当前可运行形态下的 V1 只读诊断链路，不代表 AIO-66 服务镜像拆分后的镜像、部署或服务编排验收。

| 链路 / 场景 | 前置条件 | 期望结果 | 覆盖 |
| --- | --- | --- | --- |
| hermes -> gateway: 只读 K8s 请求契约 | Hermes tool call 提供 `cluster_id`、`namespace`、`argv`、`reason`、`request_id`、`correlation_id` | gateway 返回 `result.envelope.v1`，成功时包含 stdout/audit_ref；失败时返回受控 `error.code` | `tests/test_v1_functional_baseline.py::test_hermes_gateway_k8s_read_contract_success_and_auth_failure` |
| gateway -> connector: CommandEnvelope / ResultEnvelope | gateway 构造 `CommandEnvelope(v1)`，connector 校验 cluster、namespace、grant、argv 与资源限制 | 合法命令被接受并能 round-trip；connector 离线或非法 envelope 返回受控失败，不穿透异常 | `tests/test_v1_functional_baseline.py::test_gateway_connector_contract_accepts_result_and_degrades_when_connector_offline` |
| Prometheus `query_metrics` 成功路径 | 使用 fake runner 返回 payment-api 5xx/error-rate 时序 | 返回 `ToolEnvelope`，含 `evidence_refs`、query digest、series summary 与 audit | `tests/test_v1_functional_baseline.py::test_payment_api_error_rate_spike_produces_structured_diagnosis` |
| Prometheus `query_metrics` 降级路径 | 后端不可用或超时 | 返回 `failed`，错误码为 `backend_unavailable` 或 `timeout` | `tests/test_prometheus_query_facade.py` |
| Loki `query_logs` 成功路径 | 使用 fake runner 返回 payment-api 或 CrashLoop 日志 | 返回 `ToolEnvelope`，含 `evidence_refs`、pattern grouping、samples/ref | `tests/test_v1_functional_baseline.py::*spike*` |
| Loki `query_logs` 降级路径 | 后端不可用、查询拒绝、成本超限、空结果 | 返回受控 `ToolEnvelope`，记录错误码或空结果 summary | `tests/test_loki_query_facade.py` |
| K8s read 成功路径 | fake `kubectl` 返回 Pod/describe/logs 输出 | 返回 `result.envelope.v1`，不执行 mutation，审计字段保留 request/correlation/task/command | `tests/test_v1_functional_baseline.py::test_pod_crashloop_spike_uses_k8s_logs_and_requires_approval_for_mutation_advice` |
| K8s read 降级路径 | 鉴权失败、connector/backend 不可用、超时、非法 argv、空结果 | 返回受控失败 envelope，错误码为 `permission_denied`、`backend_unavailable`、`timeout`、`command_rejected` 等 | `tests/test_k8s_tools.py` |
| Topology 成功路径 | topology store 中存在 service 与 weak dependency edge | 返回 `ToolEnvelope`，source=`topology` 的 evidence ref 指向 service topology | `tests/test_v1_functional_baseline.py::test_payment_api_error_rate_spike_produces_structured_diagnosis` |
| Topology 降级路径 | service 不存在或 store 仅能返回空 topology | 返回 `partial`，warnings 包含 `service_not_found`，作为低置信/补证输入 | `tests/test_topology_store.py` |
| Spike: payment-api 错误率升高 | fake Prometheus/Loki/Topology/K8s read 证据齐全 | 诊断输出包含 `evidence_chain`、`root_cause_candidates`、`confidence`、`recommended_actions`，mutation 建议 `approval_required=true` 且 `execute_automatically=false` | `tests/test_v1_functional_baseline.py::test_payment_api_error_rate_spike_produces_structured_diagnosis` |
| Spike: Pod CrashLoopBackOff | fake K8s describe/logs 与 Loki 证据齐全 | 诊断输出定位 workload crash loop，mutation 建议必须审批，不真实执行 mutation | `tests/test_v1_functional_baseline.py::test_pod_crashloop_spike_uses_k8s_logs_and_requires_approval_for_mutation_advice` |

## INCONCLUSIVE 缺口

- 未验证 AIO-66 拆分后的镜像构建、compose/K8s 编排、服务发现、端口与健康检查。
- 未连接真实 Prometheus、Loki、Kubernetes API、Feishu/Hermes gateway 进程；外部依赖均由 fake runner 或 mock subprocess 覆盖。
- 未验证真实 Hermes LLM tool-calling 行为，仅验证 Hermes 可调用的 V1 tool/facade 契约与结构化诊断 runtime 输出。
