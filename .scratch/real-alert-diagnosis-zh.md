# 真实告警诊断中文化

- Module：Diagnosis 的结构化诊断输出；公开 Interface 为 `run_diagnosis_session()` 返回的 `diagnosis` 与 Gateway Workbench 投影。
- Gateway Module：Evidence Decision；公开 Interface 为 `record_diagnosis_facts()` / `project()`。
- Console Module：Incident Workbench；公开 Interface 为 Workbench 页面渲染。
- 定向测试：`tests/test_diagnosis_llm_tooluse.py`、`tests/test_incident_diagnosis.py`、`tests/test_gateway_diagnosis_delivery.py`、Console Workbench 测试、`tests/test_pilot_observability_integration.py::test_real_prometheus_alertmanager_gateway_and_mcp_path`。
- 体量说明：`toolsets/incident_diagnosis.py` 开始时 1055 行，只替换本任务涉及的用户可见文案，完成时不得增加行数；真实集成测试文件 658 行，保留单一 O01 观测链路职责。

## 2026-07-22 diagnostic run 结果

- 运行分类：Diagnostic Evidence Bundle，不是 Clean Acceptance Run，不具备发布或晋级含义。
- Incident：`incident-e38df3ea50ba4183805f5f46b288a236`。
- Investigation `investigation-40da85fa6c4945ce8c1ef179943775ab`：Prometheus 基线返回 `4/4` 条时序，Loki 查询匹配 `9` 行，Connector 返回实时 Kubernetes 资源；最终因 Model Provider `invalid_response` 失败。
- Investigation `investigation-c4039e3871fc4559b9494e9b4a1df1d0`：Prometheus 基线返回 `5/5` 条时序，Loki 基线匹配 `4` 行，Kubernetes 读取成功；模型继续扩张到 63 个 Evidence Steps 后再次以 `invalid_response` 失败。
- 同一 Model Provider revision 的独立验证可恢复为 `verified / available`，但完整长上下文诊断仍复现 `invalid_response`；因此真实观测数据空缺已修复，真实模型终态尚未通过。
- 所有临时 ConfigMap、源码 `subPath` 挂载、Deployment annotation 与 `aiops-verification` namespace 均已删除；集群已恢复原始 immutable image digest。

## 2026-08-12 告警链路修复门禁

- Diagnosis Job Module：`diagnosis_service/jobs.py`，公开 Interface 为 `DiagnosisJobs.run_writeback_once()` 与 `start_workers()`；文件开始与完成均为 535 行，保持 durable execution/writeback 单一 owner 职责。定向 selector 为 `tests/test_diagnosis_jobs.py`。
- Diagnosis Runtime Module：`diagnosis_service/runtime.py`，公开 Interface 为 `run_diagnosis_job()`；Gateway Diagnosis Delivery 与 scoped Chat 负责冻结 capability snapshot，Runtime 只允许 Gateway-owned `run_k8s_read` 使用冻结 Incident scope，Registry-owned metrics、logs、topology 继续要求 exact Integration revision。
- `tests/test_diagnosis_service.py` 从 599 行增至 679 行，继续覆盖 Diagnosis HTTP/runtime 公开 contract，新增内容锁定 native capability、冻结 scope 与直接消费行为，未拆分为转发测试文件；定向 selector 为 `tests/test_diagnosis_service.py`。
