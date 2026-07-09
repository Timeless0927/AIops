# V1 功能测试矩阵

本矩阵覆盖当前 split-service 后端的只读诊断链路、writeback 和受控降级。

| 链路 / 场景 | 前置条件 | 期望结果 | 覆盖 |
| --- | --- | --- | --- |
| diagnosis -> gateway: 只读 K8s 请求契约 | Diagnosis tool call 提供 `cluster_id`、`namespace`、`argv`、`reason`、`request_id`、`correlation_id` | Gateway 返回 `result.envelope.v1`，成功时包含 stdout/audit_ref；失败时返回受控 `error.code` | `tests/test_v1_functional_baseline.py` |
| gateway -> connector: CommandEnvelope / ResultEnvelope | Gateway 构造 `CommandEnvelope(v1)`，Connector 校验 cluster、namespace、grant、argv 与资源限制 | 合法命令被接受并能 round-trip；Connector 离线或非法 envelope 返回受控失败 | `tests/test_v1_functional_baseline.py` |
| Prometheus `query_metrics` | fake runner 或 MCP backend 返回指标数据、不可用或超时 | 返回 `ToolEnvelope`；成功时含 evidence refs，失败时含受控错误码 | `tests/test_v1_functional_baseline.py`, `tests/test_prometheus_query_facade.py` |
| Loki `query_logs` | fake runner 或 MCP backend 返回日志、空结果、拒绝或超时 | 返回 `ToolEnvelope`；成功时含 grouped samples/ref，失败时不穿透异常 | `tests/test_v1_functional_baseline.py`, `tests/test_loki_query_facade.py` |
| K8s read | fake `kubectl` 或 Connector 返回 Pod/describe/logs 输出 | 返回 `result.envelope.v1`，不执行 mutation，审计字段保留 request/correlation/task/command | `tests/test_v1_functional_baseline.py`, `tests/test_k8s_tools.py` |
| Topology | topology store 存在 service/dependency，或 service 不存在 | 成功时返回 topology evidence；缺失时返回 `partial` 和 `service_not_found` warning | `tests/test_topology_store.py` |
| Payment API 错误率升高 | fake Prometheus/Loki/Topology/K8s evidence 齐全 | 输出 `evidence_chain`、`root_cause_candidates`、`confidence`、`recommended_actions`；mutation 建议必须 `approval_required=true` 且 `execute_automatically=false` | `tests/test_v1_functional_baseline.py` |
| Pod CrashLoopBackOff | fake K8s describe/logs 与 Loki evidence 齐全 | 诊断定位 workload crash loop，mutation 建议必须审批，不真实执行 mutation | `tests/test_v1_functional_baseline.py` |
| Diagnosis writeback | Diagnosis service 与 Gateway 配置相同 `AIOPS_GATEWAY_WRITEBACK_SECRET` | `POST /diagnosis/writeback` 与 `GET /incidents/{incident_id}` 要求 `X-AIOPS-Writeback-Signature` HMAC；认证成功后 Gateway durable incident row 可取回 diagnosis JSON/Markdown/summary/confidence/diagnosed_at 与 timeline refs | `tests/test_diagnosis_service.py`, `tests/test_gateway_alertmanager_webhook.py` |
| Writeback 失败记录 | Gateway 不可用、未配置 writeback secret、或签名错误 | Diagnosis HTTP export 继续可用；失败信息保留在 session export 和 timeline export 的 `writeback` 字段 | `tests/test_diagnosis_service.py`, `tests/test_gateway_alertmanager_webhook.py` |
| Split service packaging | Dockerfile、Compose、K8s manifests 和 CI matrix 指向独立服务目标 | 无 all-in-one target，无 frontend image，无整仓 COPY；服务健康检查和 smoke 可启动 | `tests/test_split_service_packaging.py`, `tests/test_k8s_manifests.py` |
