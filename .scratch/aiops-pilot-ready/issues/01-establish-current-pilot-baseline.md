Type: task
Status: resolved

# Establish Current Pilot Baseline

## Question

Against the agreed Pilot-ready workflow, what does the current repository and deployed development environment actually complete through public product boundaries, where does each stage stop, and what single reproducible command or browser action demonstrates each finding?

The answer must distinguish implemented code, passing fake-backed contract tests, real deployed behavior, missing configuration, and genuinely missing product capability. It must not treat direct SQLite writes or test-only helpers as a working stage.

## Answer

基线在 2026-07-12 清理完成后重新采样，Kubernetes context 为 `kubernetes-admin@cluster.local`。集群中没有 AIOps namespace、workload、Service、PVC、Ingress、Secret、ConfigMap、AlertmanagerConfig、ServiceMonitor 或 PrometheusRule。此前临时部署的任何观测均作废，不计入当前能力；本结论不读取或写入 SQLite。

### 总结

当前仓库 HEAD 已实现大部分 V1 领域 contract，但干净集群尚未安装任何 AIOps release，也不存在可访问的产品边界。因此真实部署行为为零，Pilot 工作流在安装前停止，后续阶段均没有真实端到端完成记录。

仓库侧定向验证通过：60 个后端测试、5 个 Console 测试和 Console production build。但关键 happy-path 测试使用临时 SQLite、fake Diagnosis HTTP server、fake internal auth、fake Notification provider，部分测试直接修改数据库状态推进 recovery/report；这些只证明代码和 contract，不是部署验收。

### 分类

| 分类 | 当前事实 |
| --- | --- |
| 已实现代码 | V1 local auth、身份与资源管理、Alert Signal/Incident、Diagnosis Request/writeback、Evidence Gate、Approval/Connector Command、recovery lifecycle、Notification Engine/admin、Incident Report 和对应 Console 页面均存在于 HEAD。 |
| fake-backed contract | 定向测试全部通过，但诊断、通知、Connector terminal result、recovery/report readiness 均有 fake、monkeypatch、临时数据库或直接 SQL 参与。 |
| 真实部署行为 | 无。集群没有 AIOps 运行资源或公开产品入口，不能对 login、Alert Signal、diagnosis、Approval、mutation、recovery、notification 或 report 作运行时声明。 |
| 缺失配置 | clean baseline 尚未创建安装输入、bootstrap credential、model credential、Notification Destination 或 ingress 配置。这是“未安装/未配置”，不能据此判断配置 contract 成败。 |
| 缺失产品能力 | 没有一个可直接安装的完整 immutable Kustomize release：RC overlay 的 Notification digest 为全零、Console 在单独 overlay 中且 digest 也为全零；bundled Prometheus/Loki 是 Python API compatibility backend，不是真实 bundled collection；Console/Gateway 没有 Web model configuration、setup flow 或 Platform Status/readiness contract。 |

### 逐阶段停止点

| Pilot 阶段 | 仓库与测试 | 当前真实停止点 | 单一复现动作 |
| --- | --- | --- | --- |
| 1. 版本化 Kubernetes 安装 | Kustomize base/overlays 可渲染，manifest/packaging 测试通过。测试的 `IMAGE_DIGESTS` 未覆盖 Notification 和 Console。RC overlay 的 Notification digest 为全零，独立 Console overlay 的 digest 也为全零，尚不能形成完整 release。 | 没有 release 被应用，真实流程在安装前停止。 | `kubectl get deploy,sts,svc,pvc,ingress -A -l app.kubernetes.io/part-of=aiops-sre-agent`（应为 No resources found） |
| 2. Console 与登录 | HEAD 有 bootstrap cookie session、CSRF、actor contract 和 LoginPage；测试使用临时 admin password/SQLite。 | 无 Console、Gateway 或公开 HTTP 入口，不能执行登录。 | `kubectl get svc -A -l app.kubernetes.io/name=aiops-console`（应为空） |
| 3. Web setup 与外部集成 | `/admin` 已有身份、Team、Resource Catalog、Connector、Approval Authority 和 Notification 管理；Notification contract 使用本地 fake Engine/provider。 | 无可进入的 Console/V1；没有 Web model configuration、resumable setup 或整体 Platform Status。 | `rg 'AIOPS_MODEL|model' apps/aiops_k8s_gateway api/openapi/gateway-v1.json apps/aiops_console_web/src --glob '!**/schema.d.ts'`（应为 0 matches） |
| 4. 真实 metrics/logs | MCP Adapter 和 evidence 代码存在；当前 bundled profile 仅提供 Python API compatibility backend 与 synthetic log Job，不满足真实 bundled collection。 | 无 AIOps observability workload，也没有通过产品边界取得 evidence。 | `kubectl get deploy -A -l app.kubernetes.io/name=aiops-dev-prometheus`（应为空） |
| 5. 受控 Alert Signal | Alertmanager webhook 鉴权、dedup 和 Incident contract 已实现；本地测试使用构造 payload。 | 无 Gateway、AlertmanagerConfig 或受控规则，无法产生或接收产品 Alert Signal。 | `kubectl get alertmanagerconfigs.monitoring.coreos.com -A -l app.kubernetes.io/part-of=aiops-sre-agent`（应为 No resources found） |
| 6. evidence-grounded diagnosis | HEAD 的 durable handoff、MCP evidence、writeback 和 Evidence Gate contract 存在。HTTP happy-path 测试注入 fake Diagnosis 和 fake AI result。 | 无 Diagnosis、MCP、Gateway 或 model configuration，不能产生真实 evidence artifact。 | `kubectl get deploy -A -l app.kubernetes.io/name=aiops-diagnosis`（应为空） |
| 7. 显式批准的隔离 restart | HEAD 可原子创建 Approval、Execution Grant、Connector Command，Connector 有 bounded restart；测试直接构造 catalog/evidence 并伪造 terminal result。 | 无 Gateway、Connector、Approval 或 verification namespace，不能发起受控 mutation。 | `kubectl get deploy -A -l app.kubernetes.io/name=aiops-connector`（应为空） |
| 8. recovery 与 Incident resolution | Recovery Observation/stabilization/reopen 代码存在。测试通过直接调用 domain service 及 SQL 更新 Investigation 状态完成。 | 无 Alert Signal 或 Incident，自然没有真实 recovery observation、稳定窗口或 resolution。 | 与阶段 5 相同的 AlertmanagerConfig 查询。 |
| 9. Notification Delivery | 独立 Notification Engine、Destination/Route/Template/Delivery/dead-letter 代码和 fake-backed contract 已实现。 | 无 Notification Engine、Destination 或 provider configuration，没有真实 Notification Delivery。 | `kubectl get deploy -A -l app.kubernetes.io/name=aiops-notification`（应为空） |
| 10. Incident Report 发布 | HEAD 有 draft/edit/publish 与 immutable history，Console 页面可 build。测试直接写 SQL 把 Incident/Investigation 推到 ready。 | 无 Gateway、Incident 或 resolved lifecycle，因此没有可发布报告。 | `kubectl get deploy -A -l app.kubernetes.io/name=aiops-gateway`（应为空） |

### 验证记录

```text
rtk test pytest -q tests/test_k8s_manifests.py
26 passed

rtk test pytest -q tests/test_gateway_v1_auth_contract.py tests/test_gateway_v1_admin_contract.py tests/test_gateway_alertmanager_webhook.py tests/test_gateway_v1_incident_contract.py
10 passed

rtk test pytest -q tests/test_gateway_v1_approvals_contract.py tests/test_gateway_v1_notification_contract.py tests/test_gateway_v1_report_contract.py
5 passed

rtk test pytest -q tests/test_notification_deployment.py tests/test_split_service_packaging.py tests/test_docker_image_workflow.py
19 passed

cd apps/aiops_console_web && rtk npm test -- --run
5 passed

cd apps/aiops_console_web && rtk npm run build
passed
```

这些发现均已由现有后续票覆盖：安装完整性归 `Define Kustomize Release Installation Contract`，Web 配置归 `Define Web Setup And Integration Ownership`，真实观测归 `Define Bundled Observability And Alert Path`，model/notification readiness 归 `Define Model And Notification Readiness`，真实 restart/recovery/report 验收归后续 controlled scenario 与 acceptance matrix。无需新增基线子票。
