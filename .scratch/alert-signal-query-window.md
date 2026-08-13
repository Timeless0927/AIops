# 任务记录：再调查查询 Alert Signal 故障窗口

## Module / Interface / selector

- Gateway Diagnosis Delivery：`apps/aiops_k8s_gateway/diagnosis_delivery.py`（开始 489 行，单一 Delivery 职责，不拆转发文件）。公开 Interface 为 `DiagnosisDelivery._payload()` 发出的 Diagnosis Request。定向 selector：`tests/test_gateway_diagnosis_delivery.py`。
- Diagnosis handoff：`diagnosis_service/handoff.py`（开始 58 行）。公开 Interface 为 `incident_from_handoff()`。定向 selector：`tests/test_diagnosis_service.py`。
- Diagnosis 查询装配：`toolsets/incident_diagnosis.py`（开始 1055 行，已超 800；只替换窗口消费，完成时不得高于开始行数）。公开 Interface 为 `build_tool_arguments()`。定向 selector：`tests/test_diagnosis_llm_tooluse.py`。
- 不改 `main.py`、Evidence Gate freshness、Model timeout、Workbench 多轮投影。

## 窗口所有权

- Request 冻结本轮 firing/reopened Alert Signal 的 `started_at` / recovered 时间 / fingerprint / status，以及由此得到的 `observation_window`。
- 调查时刻取 Investigation `created_at`，不取 Delivery 重试的 wall-clock。
- 无 Signal `started_at` 时不发明故障窗；查询层锁住旧的 now-30m 作为无信号降级，并在测试中写明。

## 完成

- Delivery 选 firing 优先的 Alert Signal，Request 带 `started_at` / `recovered_at` / `fingerprint` / `observation_window`。调查时刻用 Investigation `created_at`。
- `incident_from_handoff()` 把冻结窗投影为 `start` / `end` / absolute `time_range`。
- `build_tool_arguments()` 有冻结窗时忽略模型给出的 `start`/`end`/`time_range`；无信号时间仍用 now-30m。
- `toolsets/incident_diagnosis.py` 开始 1055 行，完成 1052 行。
- 定向测试：`tests/test_gateway_diagnosis_delivery.py`、`tests/test_diagnosis_service.py`、`tests/test_diagnosis_llm_tooluse.py`，以及直接消费方 `tests/test_gateway_mcp_diagnosis_delivery.py`、`tests/test_gateway_skill_delivery.py`、`tests/test_incident_diagnosis.py`、`tests/test_gateway_v1_incident_contract.py`、`tests/test_diagnosis_jobs.py`、`tests/test_diagnosis_runtime.py`。
