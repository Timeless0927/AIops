# 区分 Incident 恢复与 Diagnosis 结果

- Module：`apps/aiops_k8s_gateway/incident.py`（任务开始 717 行）与 `apps/aiops_k8s_gateway/investigation_events.py`。
- 公开 Interface：`IncidentService.list_incidents()`、`IncidentService.workbench()` 与 `project_latest_diagnosis_statuses()`。
- 定向 selector：`tests/test_gateway_diagnosis_delivery.py::test_incomplete_evidence_keeps_judgment_but_blocks_mutation`、`tests/test_gateway_v1_incident_contract.py::test_alertmanager_ingress_lists_incident_and_returns_workbench_snapshot`。
- Console 通过 Gateway OpenAPI Incident read model 显示独立的恢复状态与最新 Diagnosis/Evidence Gate 状态。
