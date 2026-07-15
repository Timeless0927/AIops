# Tickets: AIOps Pilot-ready Release Implementation

把 `issues/01-09` 已确定的 Pilot 决策实现为一个可从 immutable release artifact 安装、通过真实 provider 和 Kubernetes 边界完成两轮受控演练的版本；Console operational navigation 工作已合并到本图。

现有 V1 的身份、Incident/Investigation、Evidence Gate、Resource Catalog、有限 Deployment mutation、Incident Report 和 Notification Engine 是迁移基础，不重建第二套 owner。Work the **frontier**: any ticket whose blockers are all done.

## P01 建立幂等 Bootstrap 安装状态

**What to build:** Platform Operator apply release 后，Cluster 内一次性生成并持久保存 bootstrap password、Alertmanager token 和三个互相独立的 encryption key；重复 apply 不轮换，key loss 由 consumer readiness 和删除 completed Job 后的显式复核暴露。

**Blocked by:** None — can start immediately.

- [x] Bootstrap Job 使用 Kubernetes API 与 cryptographic RNG 创建缺失 Secret，不把值写入 release artifact、log 或 status。
- [x] 已存在 Secret 只校验必需 key；全部完成后写 immutable completion marker。
- [x] marker 后缺 Secret/key 返回 `bootstrap_secret_lost`，不得自动生成替代 key。
- [x] Job 使用最小 RBAC；失败恢复或 completed state 显式复核只允许删除 Job 并 reapply，已有 Secret 保持不动。
- [x] 定向 manifest/Job 测试覆盖首次创建、删除 Job 后的幂等 reapply、部分失败和 key loss。

门禁记录（P01）：Bootstrap Installation State Module 为 `bootstrap_service.py`，公开 Interface 是对 namespaced Kubernetes Secret/ConfigMap API 的幂等 reconciliation；Job/RBAC Adapter 为 `deploy/k8s/bootstrap`，复用 Gateway image，不增加独立镜像或依赖。读取权限只覆盖四个固定 Secret 和 completion marker；Kubernetes RBAC 无法按 object name 限制 `create`，因此创建权限保持 namespace-scoped。固定名称的 completed Job 不会由普通同版本 apply 重跑；失败恢复或显式复核先删除 Job 再 reapply，marker 后缺 key 返回 `bootstrap_secret_lost` 且不生成替代值，Secret consumer readiness 负责持续暴露 key loss。定向 selector 为 `tests/test_bootstrap_service.py`，直接 packaging consumer 为 `tests/test_split_service_packaging.py` 与 `tests/test_docker_image_workflow.py`。P02 负责把该资源接入固定 `aiops-system` canonical overlay、pin immutable digest，并让所有 Secret consumer fail closed。

## C01 建立共享 Console Shell 与真实导航

**What to build:** 已登录 User 在所有 Console 页面看到一致、route-aware、适配窄屏的 shell；导航只显示已交付的真实页面，不显示空 Dashboard 或 coming-soon 项。

**Blocked by:** None — can start immediately.

- [x] Production shell 统一拥有 brand、主导航、User menu、退出、role visibility 和移动菜单。
- [x] Incident、Report 与 `/admin` 页面不再复制 shell；`/admin` 只从 Platform Administrator 的 User menu 进入。
- [x] 当前 route、键盘焦点、可访问名称和 390px 无横向溢出有最小定向测试。
- [x] 不增加导航 framework、客户端权限缓存或 browser-side workflow state machine。

门禁记录（C01）：Console Shell Module 为 `apps/aiops_console_web/src/shell/console-shell.tsx`（162 行），公开 Interface 是接收已认证 Actor 的单一 route layout 与纯 `shellRoute` 路由投影；`App.tsx` 只查询一次 Actor 并通过 nested Route 装配 shell，Incident list、Workbench、Incident Report 和 `/admin` 只拥有页面内容，不再创建 header 或权限缓存。主导航当前只公开已交付的 `事件`，Platform Administrator 的 `/admin` 入口只存在于 User menu，非管理员 Route 继续 fail closed；移动端复用现有 Sheet，User menu 复用现有 Base UI 依赖生成的 shadcn Dropdown Menu，不增加 framework、依赖或浏览器状态机。定向 selector 为 `apps/aiops_console_web/src/shell/console-shell.test.tsx`，覆盖 active route、Report/Workbench back target、原生可聚焦控件、可访问名称和 390px 防溢出约束；Console Vitest 共 8 passed、TypeScript no-emit 与 production build 通过，直接 artifact consumer `tests/test_console_delivery.py` 6 passed。新增生产文件与测试文件均低于 500 行。

## K01 接收自然语言 Change Request 并生成 Plan Revision

**What to build:** User 可以从 Incident 提交 desired outcome/context，模型基于脱敏 live facts 生成 immutable Change Plan revision；模糊请求一次询问一个 blocking question，不产生可审批 proposal。

**Blocked by:** None — can start immediately.

- [x] Gateway-owned Change Request/Plan Module 持久化 request、phase state、immutable revisions 和 transition events。
- [x] User 不提交 YAML、JSON Patch 或 executable proposal；明显 credential 被拒绝并引导 Secure Input。
- [x] target、desired state、scope 或 post-check 模糊时进入 `needs_input`，新输入 supersede 旧 revision。
- [x] 模型只收到 sanitized discovery/facts 且无 Kubernetes credential；raw reasoning 不持久化。
- [x] `/api/v1`、Workbench UI 与 contract tests 覆盖 planning、needs_input、superseded 和 authorization。

门禁记录（K01）：Change Request/Plan Module 为 `apps/aiops_k8s_gateway/change_requests.py`（547 行），公开 Interface 是提交 Change Request、追加 blocking input、显式 retry planning 和读取 actor-scoped projection；它独占 request、active Phase、immutable revision/input/event 的 `gateway.db` 状态，Change Request status 只投影 active Phase，migration 17 单独持久化 retry idempotency。Incident Module `incident.py` 的新增公开 Interface 是 actor-scoped sanitized planning facts projection，定向 selector 为 `tests/test_gateway_v1_change_requests_contract.py` 与 `tests/test_gateway_incidents.py`；该文件从任务开始 727 行增至 750 行，保持低于 800 行。Diagnosis model Module 为 `diagnosis_service/change_planner.py`，内部 HTTP Adapter 为 `change_planner_http.py`；producer 与 Gateway consumer 复用 `aiops/contracts/change_planning.py` 的结构化 contract，只接收 sanitized facts，不持有 Kubernetes credential，也不持久化 model reasoning。Gateway/Console Adapter 为 `change_request_http.py`、generated OpenAPI types 与 `changes/change-requests-section.tsx`（151 行）；`workbench-page.tsx` 从任务开始 480 行结束为 483 行，只组合 Incident Workbench 领域 Module。定向 selector 为 `tests/test_gateway_v1_change_requests_contract.py`、`tests/test_change_planner.py`、`tests/test_diagnosis_service.py::test_post_change_plan_uses_authenticated_model_boundary` 与 `apps/aiops_console_web/src/api/client.test.ts`；直接 contract/architecture 共 40 passed，Console Vitest 6 passed、TypeScript no-emit 与 production build 通过，最终全量 pytest 为 486 passed。新文件均低于 800 行，`diagnosis_service/service_main.py` 保持 499 行且只新增 route 装配。

## P02 交付单入口 Canonical Kustomize Overlay

**What to build:** 新 Operator 可以从一个固定 `aiops-system` top-level Kustomize overlay 安装 Console、全部 control-plane process、独立 PVC、同源 NodePort 和安全网络边界，不需要源码、Helm 或本地 build。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态.

- [x] 唯一安装入口是 `kubectl apply -k` top-level overlay；所有资源本地引用且 namespace 固定。
- [x] Console、Gateway、Diagnosis、Connector、MCP、Notification 和 bootstrap 使用 immutable image digest。
- [x] Gateway/Diagnosis/Connector/Notification 使用独立 RWO PVC；Console NodePort `30088` 同源代理 auth、API 和 event stream。
- [x] 浏览器不可直连内部 process；ServiceAccount、projected token 和 NetworkPolicy 保持现有进程边界。
- [x] Installation Ready 只基于 Job/PVC/Deployment/healthz，不把未配置 integration 伪装 ready。
- [x] render、API validation、secret/PVC/route 和同版本 reapply 有定向测试。

门禁记录（P02）：Pilot Release Installation Module 为 `deploy/k8s/pilot`，公开 Interface 是固定 namespace `aiops-system` 的单一 `kubectl apply -k deploy/k8s/pilot` 入口；它组合现有 base、Console 与 Bootstrap owner manifest，并只在 overlay 删除 external Ingress/Operator CRD、固定 NodePort/edge route、必需 Secret/key consumer、Cluster-wide Change Executor RBAC 与 immutable image digest。Connector 只通过 Gateway long-poll worker 接受 command，未认证的 direct HTTP execution surface 已删除，deny-all ingress 与只读 discovery non-resource RBAC 随 broad credential 一起安装。Gateway、Diagnosis、Connector 和 Notification 各自保持 single replica、独立 RWO PVC；O01/O02 后续在同一 overlay 加入 Prometheus/Loki 与剩余 20Gi，不在本票伪造 observability readiness，P03 负责把本地 owner manifest 打包成自包含 tarball。Console、七个服务 image 和 Bootstrap 复用的 Gateway image 由 Actions run `29223743324`、`29223542879`、`29224280108`、`29224665790` 发布并逐一通过匿名 `imagetools inspect`；服务矩阵与 compose smoke 成功。定向 selector 为 `tests/test_pilot_release.py`，直接 consumer 为 `tests/test_connector_registration_recovery.py`、`tests/test_bootstrap_service.py`、`tests/test_console_delivery.py`、`tests/test_k8s_manifests.py`、`tests/test_split_service_packaging.py` 与 `tests/test_docker_image_workflow.py`，共 72 passed；同一 render 另通过 Kubernetes API Server dry-run，未写入 Cluster。新生产 manifest 与测试文件均低于 500 行。

## S01 完成多 Connector Enrollment 与 Read Verification

**What to build:** Platform Administrator 可以同时管理多个 exact Connector/Cluster Enrollment，凭据只显示一次；Cluster 只有在 heartbeat 和 bounded live read verification 都成功时才 ready，并能安全轮换 credential。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay.

- [x] unique `connector_id/cluster_id` pair 并行存在，任一 identity 冲突 fail closed。
- [x] 注册/heartbeat 只证明连接；read verification 冻结 Cluster identity、discovery 和 permission summary。
- [x] rotation pending 停止新 grant/command，旧 credential 保持到 candidate 首次成功注册后原子切换。
- [x] started mutation、Unknown Outcome 或 unfinished rollback 阻止 rotation；安全 disable 保留 reconciliation。
- [x] Console 管理、公开安全 status、fresh-auth、one-time credential 和 restart recovery 有端到端测试。

门禁记录（S01）：Connector Enrollment/Cluster Module 为 `apps/aiops_k8s_gateway/connector_enrollments.py`（719 行），公开 Interface 是创建/更新 Enrollment、exact pair registration、heartbeat、Cluster governance、admin/public safe projection 与 Connector Command terminal result verification；它独占 migration 3/18 的 Enrollment、candidate credential hash、Cluster presence 和 revision-bound read verification state，共享 Gateway transaction 但不拥有 Command lifecycle。行为不变迁移先以提交 `9529bb9` 把该完整能力从 973 行 `v1_store.py` 移出并将后者降至 655 行；随后行为提交复用既有 `get_resource`/Connector journal 路径执行 bounded `kubectl get pods -o json`，只冻结 authenticated pair、Kubernetes `apiVersion/kind` 和实际成功的 namespace list permission summary，不建立第二套执行状态机。`connector_commands.py` 从任务开始 704 行增至 763 行，公开 Interface 只增加 transaction-scoped verification queue/result hook，并在 verified/current Enrollment gate 普通 read、grant 和 dispatch；`main.py` 从 632 行增至 660 行且只增加装配/路由，新的 safe status 位于 31 行 HTTP Adapter。`tests/test_gateway_v1_connectors_contract.py` 从 417 行增至 526 行，定向 selector 为该文件、`tests/test_connector_enrollments.py`（200 行）、`tests/test_gateway_connector_commands.py` 和 `tests/test_gateway_v1_approvals_contract.py`；覆盖多 pair 冲突、真实 read 成败与显式 retry、staged rotation/candidate expiry、started/Unknown Outcome/unfinished rollback blocking、disable、one-time credential、普通 User safe status 和同一数据库 restart recovery。Gateway/Connector 直接消费者共 71 passed；Console Vitest 8 passed、TypeScript no-emit 与 production build 通过。所有新增文件低于 800 行。

## S02 配置并验证真实 Model Provider

**What to build:** Platform Administrator 可以在 Web 保存一个 OpenAI-compatible Provider revision，并通过真实两轮 tool-use/nonce probe 验证；只有 exact ready revision 才能启动新 Diagnosis。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态; C01 建立共享 Console Shell 与真实导航.

体量门禁（S02-1）：Diagnosis Model Provider Module 的纯 owner 为 `diagnosis_service/model_provider.py`（393 行），公开 Interface 是保存/删除 immutable encrypted configuration revision、创建/执行 durable verification、投影 masked detail/public status、按 exact revision 提供运行配置与记录 bounded availability；SQLite Adapter `model_provider_repository.py`（301 行）独占 `diagnosis.db` migration 3/4/6、transaction、lease 与 row persistence，credential Adapter `model_provider_crypto.py`（41 行）独占 AES-GCM/key material。共享 SQLite connection、migration 和 foreign-key 约束位于 `diagnosis_service/database.py`（38 行），不共享领域 Store Interface；`service_main.py` composition root 直接注入 Repository、cipher、clock 与 cryptographic ID source，不增加单实现 factory。Jobs Module `diagnosis_service/jobs.py` 从 475 行增至 492 行，只冻结 `provider_revision`、持久化受限 partial Evidence，并对明确 no-retry Provider/contract failure terminal。OpenAI-compatible Adapter `diagnosis_service/diagnosis_provider.py` 从 292 行增至 537 行，公开 Interface 为 configured Provider `chat_with_tools` 与两轮 `run_readiness_probe`，集中执行 endpoint scope、pinned DNS/TLS/no-redirect transport、strict tool-call/JSON/nonce probe；bounded error taxonomy 由 Model Provider owner 统一约束，Adapter 不持久化状态。

体量门禁（S02-2）：行为不变迁移提交 `cab8b26` 把完整 LLM tool-use session、structured final JSON parsing、Evidence step accumulation 与 Provider failure propagation capability 从任务开始 1482 行的 `toolsets/incident_diagnosis.py` 迁入 `toolsets/diagnosis_session.py`（471 行）；旧文件现为 Evidence/Result owner（1055 行），直接公开其 owned implementations `build_tool_arguments`、`collect_tool_observation`、LLM/fallback result composition、status 与 persistence，最终不保留 wrapper、双路径、compatibility export 或跨 Module 私有导入。Diagnosis Runtime Module 为 `diagnosis_service/runtime.py`（127 行），公开 Interface 是 ready revision admission、exact revision Provider binding、Job execution 和 readiness probe；max turns 与 monotonic clock 由装配层注入。`service_main.py` 从 499 行增至 577 行，仅保留 HTTP route、Adapter、owner/worker 装配与进程启动，包括 Model Provider composition 与 verification worker thread。Gateway `main.py` 从 769 行增至 782 行，只装配 144 行 Model Provider HTTP Adapter，仍低于 800 行且不新增领域决策；Gateway 的 reason/fresh-auth/audit 校验与 Diagnosis internal Adapter 的 exact-field 校验分别位于各自 trust boundary，不复制 Provider error taxonomy。`tests/test_diagnosis_service.py` 从 403 行增至 541 行，通过 runtime/HTTP/owner 公开边界验证；`tests/test_incident_diagnosis.py` 任务开始与结束均为 1003 行，只验证 Evidence/Result 公开 Interface，两者均不依赖私有调用顺序。

- [x] Diagnosis 独占 encrypted credential、endpoint/model/timeout、revision 和 verification record；Gateway 不复制配置。
- [x] external/cluster-internal endpoint 分别执行 SSRF、DNS rebinding、redirect 和 TLS policy。
- [x] test API durable 返回 operation identity；tool call、structured JSON 与 nonce 任一不匹配均 `invalid_response`。
- [x] credential/config change 使旧 verification stale；确定性 rejection failed，瞬时故障只降 availability。
- [x] Diagnosis 冻结 provider revision，失败明确结束且不回退 keyword diagnosis、不自动重跑。
- [x] 正常产品配置不再读取 `AIOPS_MODEL_*` environment fallback；该路径只允许定向测试使用。
- [x] Admin UI、public safe status、encryption、revision concurrency 和 provider error taxonomy 有定向测试。

验收（S02）：Diagnosis owner/session/runtime、Gateway contract、deployment/packaging 和 architecture direct consumers 173 passed；Gateway Model Provider/OpenAPI/Console delivery contract 7 passed；Console focused Vitest 8 passed、全量 Vitest 40 passed，TypeScript no-emit 与 Vite production build 通过，desktop/390px mock 预览无横向溢出或控件裁切。最终全量 pytest 639 passed/2 skipped。Provider transient re-test 保留 exact revision 的有效 verified record 并只降低 availability；任意 final structured JSON contract failure 都写 `invalid_response/unavailable` 且不重跑；后期 Provider failure 的 durable failed writeback 保留已采集 Evidence steps；非 JSON HTTP error body 仍按 status 投影 bounded taxonomy。仅保留既有 >500 kB 主 bundle warning。

## S03 把 Notification Test Delivery 绑定到 Revision Readiness

**What to build:** Platform Administrator 可以验证 exact Notification Destination revision 并显式选为 Pilot Route；只有真实 durable test Delivery `sent` 才 ready，修复 credential 后 pending work 安全恢复。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态; C01 建立共享 Console Shell 与真实导航.

体量门禁（S03）：Notification Request/Delivery Module 为 `notification_service/requests.py`（任务开始 713 行），公开 Interface 是 durable Request 接收、Delivery lease/attempt/retry/terminal transition 与结果查询；本票只增加 exact Destination revision 冻结、test Delivery 接收和 readiness terminal hook，不把 Destination configuration、routing 或 Web setup 状态迁入该 Module。schema migration 先进入该文件，随后以行为不变搬迁把完整 SQLite schema/migration infrastructure 移入 `notification_service/database.py`（265 行）并删除旧实现，`requests.py` 降至 501 行；Database Module 只公开 `migrate_notification_database`，不共享领域 Store Interface。其公开 Interface 回归文件 `tests/test_notification_service.py` 任务开始 525 行，定向 selector 为该文件；直接 consumer 为 `tests/test_notification_configuration.py`、`tests/test_gateway_v1_notification_contract.py` 与 Console Notification Admin 测试。schema migration、行为不变搬迁与行为实现分独立提交，生产文件与测试文件结束时均保持低于 800 行。

体量门禁（S03 Configuration）：Notification Destination/Route Configuration Module 为 `notification_service/configuration.py`（任务开始 463 行），公开 Interface 是 encrypted Destination revision 管理、exact verified Pilot Route 选择与 first-match routing；readiness 投影独立位于 `notification_service/destination_readiness.py`，不拥有 Delivery attempt 状态机。定向 selector 为 `tests/test_notification_destination_readiness.py` 与 `tests/test_notification_configuration.py`，直接 HTTP consumer 为 `tests/test_gateway_v1_notification_contract.py`；该生产文件只保留一个内聚 configuration/routing owner 并保持低于 800 行。

体量门禁（S03 Gateway）：Gateway composition root `apps/aiops_k8s_gateway/main.py` 任务开始 782 行，公开 Interface 仍为进程 HTTP route dispatch 与依赖装配；本票只把独立 `notification_admin_http.py` Adapter 接入 `_request_session`，不新增领域决策、SQL 或外部调用编排。定向 selector 为 `tests/test_gateway_v1_notification_contract.py`，文件结束时保持低于 800 行。

体量门禁（S03 Gateway Audit）：Gateway V1 Audit owner `apps/aiops_k8s_gateway/v1_store.py` 本次触碰前 660 行，公开 Interface 是 immutable admin audit 写入、最近记录投影与按 target/action 查找尚未对账的 `outcome_unknown` request；本票只增加基于既有 `admin_audit` ledger 的 unresolved query，不新增配置或业务状态 owner，结束为 682 行。定向 selector 为 `tests/test_gateway_v1_notification_contract.py` 与 `tests/test_notification_configuration.py::test_gateway_proxy_strips_reason_and_audits_only_masked_engine_result`。

- [x] Destination revision change 使 verification stale 并暂停 pending Delivery，不消耗 attempt。
- [x] test 复用真实 Notification Delivery/Apprise path、bounded retry 和 operation identity，不建第二套测试状态机。
- [x] terminal `sent` 才 verified；dead-letter/credential rejection 使用安全 reason code且不假成功。
- [x] verified Destination 仍需管理员显式选择 Pilot catch-all Route；测试本身不改 routing。
- [x] Admin UI、public safe status、masked audit、revision concurrency 和 restart recovery 有定向测试。

验收（S03）：Notification Test 使用真实 durable Delivery、最多 3 次 attempt、response-time `Retry-After`、可杀死的 10 秒 Apprise transport process 与 immutable operation ledger；只有每 revision 最新 test `sent` 且 available 才可显式选择 Pilot Route。credential/revision 变化在 claim 前后都暂停且回滚 attempt，成功复测只把非 test frozen presentation 绑定新 revision 后恢复；旧 test identity 不重绑，普通 Route 不 fallback。Gateway 对 fresh-auth/reason/revision/idempotency、64KiB owner response、masked audit 与 `outcome_unknown` 持久对账 fail closed；Console 保留同 request ID、阻止新 credential mutation并显示 credential 修复与 `next_attempt_at`。最终 `requests.py` 782 行、`configuration.py` 654 行、`destination_readiness.py` 171 行、`delivery_sender.py` 44 行、`apprise_adapter.py` 210 行、Notification `service_main.py` 160 行、Gateway `main.py` 785 行、`v1_store.py` 682 行、Console Module 240 行、S03 主测试 782 行，均低于 800 行。Notification/Gateway direct consumers 60 passed；Console 全量 Vitest 44 passed，TypeScript no-emit 与 Vite production build 通过；最终全量 pytest 659 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。仅保留既有 >500 kB bundle warning。

## O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics

**What to build:** Canonical bundle 使用真实 Prometheus、Alertmanager 和 kube-state-metrics 收集 AIOps/verification metrics、求值 rule 并以 authenticated `send_resolved` webhook 驱动 Gateway Incident。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay.

- [x] 使用 native manifests、immutable digest、独立 Prometheus PVC 和已确定 resource/7d retention；不依赖 Operator CRD。
- [x] static/file discovery 只抓取声明目标，external `cluster` label 与 Connector Cluster identity 一致。
- [x] Alertmanager route 只把明确 `aiops_route` alert 发到 Gateway，使用 bootstrap token 且不暴露外网。
- [x] target、rule、firing/resolved webhook 和 MCP guarded query 都有真实集成检查。
- [x] 删除 Python compatibility metrics、constant series、label-only alert 和 manual webhook acceptance path。

验收（O01）：Observability Metrics Module 通过 `deploy/k8s/observability` 暴露真实 Prometheus/Alertmanager/kube-state-metrics Service 与原生 config/rule Interface；canonical `deploy/k8s/pilot` 固定三个公开 image digest、Prometheus `10Gi` RWO PVC、`7d/8GB` retention、`pilot-cluster` exact identity、最小只读 RBAC 和 backend/Gateway ingress allowlist，不渲染 Operator CRD、compatibility Prometheus、`payment-api`、synthetic log Job 或 label-only smoke path。官方 `promtool` 校验 6 条 rule，`amtool` 校验 authenticated gateway-only route；server dry-run 与实际 `kubectl auth can-i` 证明 Prometheus 仅 Pod discovery、kube-state-metrics 仅 selected object list/watch，均无 Secret read/write。真实双节点 Cluster 中三个 Deployment、五个 PVC 与其余 canonical workload Ready；10 个声明 target 及 annotated Pod targets 全部 up，rules 全部 healthy，MCP guarded query 返回真实 Prometheus Evidence。opt-in 集成 selector `tests/test_pilot_observability_integration.py` 使用真正不可调度的 `aiops-verification` Deployment，严格验证本轮 firing signal、Gateway Incident、resolved signal 和新 Recovery Observation 后清理 fixture，最终 85.29 秒通过。O01/P02 与直接 Prometheus/Alertmanager/Gateway consumers 74 passed/1 skipped；最终全量 pytest 665 passed/3 skipped；Standards 与 Spec 最终复审均零 finding。当前环境 worker 直连 `registry.k8s.io` 曾超时，验收使用用户提供代理把 exact digest 预拉入 master containerd；canonical image 来源未替换，安装文档明确所有节点必须可拉取公开 digest。

## O02 部署真实 Loki 与 Alloy 日志链路

**What to build:** Canonical bundle 使用 single-binary Loki 和 per-node Alloy 从 Kubernetes Pod log API 收集真实 stdout/stderr，Diagnosis 只能经 MCP Loki guarded query 获得 evidence。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay.

体量门禁（O02 Integration）：`tests/test_pilot_observability_integration.py` 任务开始时 339 行，所属 Observability real-Cluster acceptance Module 的公开测试 Interface 是 opt-in Kubernetes workload、Prometheus/Loki HTTP 与 MCP/Gateway product boundary；O02 只增加真实 Pod stdout→Alloy→Loki→MCP、owner unavailable 和 PVC recovery 验收，定向 selector 为 `tests/test_pilot_observability_integration.py::test_real_alloy_loki_mcp_and_owner_unavailable_path`。文件结束为 525 行且仍是一个内聚的双 backend acceptance owner，不为行数制造只转发 helper，并保持低于 800 行。

- [x] Loki 使用 TSDB v13、filesystem、10Gi PVC、compactor 和 168h retention，resource limit 采用已验证基线。
- [x] Alloy DaemonSet 按 node discovery/relabel，保留 cluster/namespace/pod/container labels，不使用 hostPath、hostPort 或 aggregator。
- [x] RBAC 只覆盖 Pod discovery/log read，不读 Secret 或 Operator CRD。
- [x] Loki readiness、真实 unique log query、MCP evidence ref 和 owner unavailable error 有集成检查。
- [x] 删除 synthetic push、内存 log backend 和 collector-side business log filtering acceptance path。

验收（O02）：Observability Logging Module 通过 `deploy/k8s/observability` 暴露 Loki 3.6.3 single-binary 与 Alloy 1.12.0 per-node DaemonSet；canonical bundle 固定两个公开 image digest，Loki 使用 TSDB v13、filesystem、`10Gi` RWO PVC、compactor、`168h` retention 与 `500m/512Mi` request、`1CPU/1Gi` limit，Alloy 通过 `spec.nodeName` 分片从 Kubernetes Pod log API 拉取日志并只保留 bounded identity labels，全路径无 hostPath、hostPort、aggregator、synthetic push、内存 backend 或 collector-side business filtering。Alloy RBAC 实测只允许 Pod discovery 与 `pods/log get`，不读 Secret；Loki 无 Kubernetes token，NetworkPolicy 只允许 Alloy/MCP/Prometheus 声明流量。真实双节点 Cluster 中 Alloy `2/2`、Loki 与 `10Gi` PVC Ready 且零重启；唯一 Pod stdout 经 Alloy→Loki 查询成功，MCP guarded query 返回真实 Evidence ref，Loki scale-to-zero 时返回 `backend_unavailable`，恢复同一 PVC 后原日志仍可查询。只读 storage metrics sidecar 暴露 `aiops_storage_available_ratio{job="loki-storage",service="aiops-loki"}=0.721527` 且 Prometheus target up。Loki binary config、Alloy `validate`、server dry-run 与 RBAC checks 全部通过；真实 opt-in selector `tests/test_pilot_observability_integration.py::test_real_alloy_loki_mcp_and_owner_unavailable_path` 100.17 秒通过，O02 与直接 consumers 62 passed/2 skipped，最终全量 pytest 669 passed/4 skipped；Standards 与 Spec 最终复审均零 finding。

## C02 交付有权限范围的资源工作区

**What to build:** User 可以从 `资源` 查看自己有权访问的 Cluster、Service 和 Deployment Target，识别 offline、unbound、unavailable；管理员从同一事实 deep-link 到 `/admin` 治理。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; S01 完成多 Connector Enrollment 与 Read Verification.

体量门禁（C02-1）：Resource Catalog Module 的现有公开 Interface 为 discovery refresh、Service/Deployment Target/Binding governance、Incident/Change target resolution 与 admin `list_state`；`resource_catalog.py` 从任务开始 531 行增至 652 行，本票新增 actor-scoped safe summary Interface，不开放 admin description、governance notes、credential、audit 或其他 Team binding。Gateway 复用现有 `resource_catalog_http.py` Adapter 认证、scope 裁剪与序列化，`main.py` 保持 769 行且不新增领域决策。Console Resource Workspace Module 为 113 行，只拥有 URL filter、read projection 与管理员 `/admin` deep-link，不复制 Service/Binding edit form。定向 selector 为 `tests/test_gateway_resource_catalog.py`、`tests/test_gateway_v1_resource_catalog_contract.py`、`apps/aiops_console_web/src/resources/resource-workspace-page.test.tsx` 与 `apps/aiops_console_web/src/shell/console-shell.test.tsx`；直接 consumer 覆盖 Connector public status、既有 admin Resource Catalog contract 和 schema migration auth contract。

- [x] `/api/v1` 提供 actor-scoped Resource Catalog summary，不暴露其他 Team 或 admin-only detail。
- [x] `/resources` 展示真实 ownership/binding/runtime state，筛选和选中资源由 URL 拥有。
- [x] 普通 User 只读；管理员编辑继续由现有 owner/admin workflow 完成，不复制表单。
- [x] scope denial、offline/unbound/deleted state、OpenAPI producer/consumer 和窄屏有定向测试。
- [x] 页面可用后才把 `资源` 加入共享导航。

验收（C02）：Resource Catalog owner 以 actor Team scope 投影 Cluster、Service 与 Deployment Target 安全摘要；普通 User 只能看到所属 Team binding 及已授权 Cluster 内的 unbound discovery，Platform Administrator 可查看全部资源并 deep-link 到既有 `/admin?section=catalog` 治理。每轮 discovery refresh tombstone 未再次观察到的 candidate，并只把 discovery disappearance 投影为 Target `deleted`；Connector public status 由装配层经现有 owner Interface 注入。`/resources` 的 Cluster、Environment、Team、Service、binding/runtime state 与选中资源均由 URL 拥有，desktop/mobile 共享导航只在定向验收后接入。Resource/Connector/migration 直接消费者 20 passed，最终 migration/auth 与资源 contract 回归 5 passed；Console Vitest 39 passed，TypeScript no-emit 与 Vite production build 通过；全量 pytest 602 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。仅保留既有 >500 kB bundle warning。

## C03 交付 Incident Report 资料库

**What to build:** User 可以从 `报告` 找到有权访问的 draft 与 immutable publications，并打开现有 Incident-scoped Report workspace。

**Blocked by:** C01 建立共享 Console Shell 与真实导航.

体量门禁（C03-1）：Incident Report Module 的现有公开 Interface 为 actor-scoped Incident report `get/update/publish` 与 immutable publication；`incident_reports.py` 从任务开始 339 行增至 446 行，新增纯 summary `list_for_actor` projection，但不在列表返回 narrative、frozen facts、Evidence 或 governance history。Gateway 复用现有 94 行 `incident_report_http.py` Adapter 认证、scope 裁剪与序列化 `/api/v1/reports`，`main.py` 保持 769 行且没有新增装配。Console 新 Report Library Module 为 250 行，只拥有 URL filter、summary list 与到现有 `/incidents/:id/report` workspace 的链接，不复制 edit/publish state machine；共享 Shell 只在页面通过定向验收后增加 desktop/mobile 导航。定向 selector 为 `tests/test_gateway_incident_reports.py`、`tests/test_gateway_v1_report_library_contract.py`、`apps/aiops_console_web/src/reports/report-library-page.test.tsx`、现有 `report-page.test.tsx` 与 `shell/console-shell.test.tsx`。

- [x] Report owner 提供 actor-scoped summary list，不在列表返回完整 narrative/history payload。
- [x] `/reports` 区分 draft、latest publication 和 reopened Incident 多版本；筛选由 URL 拥有。
- [x] 编辑和 publish 只发生在现有 Report route，不复制 Report state machine。
- [x] scope non-disclosure、version ordering、empty/loading、OpenAPI 和 390px 有定向测试。
- [x] 页面可用后才把 `报告` 加入共享导航。

验收（C03）：Incident Report owner 以单一 scope SQL 与共享 draft eligibility predicate 投影纯 summary read；eligible 但尚未进入 workspace 的 Report 只标记 draft，不因列表读取批量冻结 payload，active Incident 不伪造 draft。summary 包含 Incident/Service、current draft metadata、latest publication、publication count 和 relevant time，不返回 narrative、frozen facts、Evidence 或 governance history；reopened Incident 保留 latest immutable version 与多版本数量。`/reports` 的 Incident title/ID、Service、state 与 24h/7d/30d time filter 全由 URL 拥有；从资料库进入既有 Incident-scoped workspace 时携带 filter，Shell 返回时恢复筛选，Incident-origin route 保持原返回路径。Report/Incident owner 与 HTTP/OpenAPI 直接消费者 17 passed；Console Vitest 36 passed，TypeScript no-emit 与 Vite production build 通过；全量 pytest 601 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。仅保留既有 >500 kB bundle warning。

## K02 通过 Connector 完成 Discovery Live Read 与 Server-side Dry-run

**What to build:** Change Plan 可以针对任意 exact Kubernetes GVK 获取 live object、canonicalize create/patch/delete，并在审批前由 Connector 请求真实 API Server dry-run，Console 展示 final object diff。

**Blocked by:** K01 接收自然语言 Change Request 并生成 Plan Revision; S01 完成多 Connector Enrollment 与 Read Verification.

- [x] Connector 是 discovery/live read/dry-run 的唯一 Kubernetes Adapter；Gateway/Diagnosis 无 Kubernetes credential。
- [x] create 冻结完整 JSON object，patch 使用 RFC 6902，delete 使用 DeleteOptions；拒绝 shell/free kubectl/subresource。
- [x] existing object 冻结 exact GVK/namespace/name/UID/resourceVersion 和 relevant old values。
- [x] 每个 Change 至少有一个 structured Kubernetes post-check；Prometheus/Loki predicate 通过 query guard。
- [x] dry-run object diff、precondition、policy error 与 redaction 通过 Gateway public contract 和真实 API Server 测试。

门禁记录（K02）：共享 canonical contract Module 为 `aiops/contracts/kubernetes_change.py`（387 行），公开 Interface 是严格校验 model draft 与不可信 Connector validation result；Diagnosis producer、Gateway consumer 与 generated OpenAPI contract 同步使用 typed create、RFC 6902 patch、DeleteOptions 和 structured post-check。Connector Kubernetes Adapter 为 `apps/cluster_connector/kubernetes_change_adapter.py`（317 行），公开 Interface `execute_validation_command` 是唯一 discovery/live read/`dryRun=All` 边界；它冻结 exact GVK/namespace/name/UID/resourceVersion/relevant old-value tests，拒绝 subresource/free kubectl，且对 Secret map、credential-shaped field 和 `DATABASE_PASSWORD`/`AUTH_TOKEN` env value 做 hash redaction。Delete dry-run 同时发送 query `dryRun=All` 与临时 `DeleteOptions.dryRun=["All"]`，frozen execution payload不含 dryRun。

Gateway validation owner 为 `apps/aiops_k8s_gateway/kubernetes_change_validation.py`（229 行），公开 Interface 是 begin/supersede/result/projection；它只拥有 migration 21 的 validation state。Enrollment lookup、typed command enqueue/schema 和 Change Request event/Phase transition 分别通过所属 owner 的 `validation_connector_in`、`ConnectorValidationCommands.queue_in` 和 `ChangeRequests.record_validation_result_in` transaction Interface 完成；成功进入 `awaiting_approval`，policy/transport/contract failure 回到可显式 retry 的 `planning`。`change_requests.py` 从 558 行增至 672 行，只增加 validation owner 装配、migration 20 Phase contract、event/status projection；`connector_commands.py` 763→764 仅扩展 durable read-like lifecycle 分类；`connector_enrollments.py` 719→737 只增加 verified capability lookup；`main.py` 660→683 只做依赖装配/result dispatch；`command_worker.py` 511→543 只增加 typed Adapter 分发，均低于 800。`tests/test_gateway_v1_change_requests_contract.py` 473→495，仍低于 500。

定向 selector 为 `tests/test_kubernetes_change_contract.py`、`tests/test_connector_kubernetes_change_adapter.py`、`tests/test_gateway_kubernetes_change_validation.py`、`tests/test_gateway_v1_change_requests_contract.py`、`tests/test_change_planner.py` 与 `apps/aiops_console_web/src/changes/change-requests-section.test.tsx`；Connector/Gateway/Diagnosis/architecture/package 直接消费者最终 132 passed、Console Vitest 9 passed、TypeScript/Vite production build 通过。显式启用 `AIOPS_RUN_KUBERNETES_INTEGRATION=1` 后，真实 API Server create/patch/delete dry-run 2 passed，并以连续 live UID/resourceVersion 一致证明无 mutation。诊断初版 delete 仅发送 query dryRun，曾使 `default/kube-root-ca.crt` 被真实删除；Kubernetes controller 已自动重建（UID 变化），随后修正 DeleteOptions body、使用临时 probe 复现/清理，并完成上述无 mutation 回归验证。

## K03 审批 Exact Change Plan Phase

**What to build:** 有明确 Object、Namespace、Service 或 Cluster Change Authority 的 User 可以审阅并审批一个 frozen Phase；Platform Administrator/Team Membership 不隐含权限。

**Blocked by:** K02 通过 Connector 完成 Discovery Live Read 与 Server-side Dry-run.

- [x] Authority 与 Environment 相交，并在 proposal、diff read、Approval、grant 和 dispatch 时重复校验。
- [x] Approval 冻结 ordered Changes、dry-run diff/hash、risk、post-check、rollback policy 和 phase expiry。
- [x] fresh auth、reason 和 exact target confirmation 满足后才进入 approved；同一授权 User 可 self-approve。
- [x] dry-run 10m、approved start 15m 到期显式 expired，不自动 refresh/reapprove。
- [x] 无 Authority 的 diff non-disclosure、stale revision、idempotency 和 immutable audit 有 contract/UI 测试。

门禁记录（K03）：Kubernetes Change Authority owner 为 `apps/aiops_k8s_gateway/kubernetes_change_authorities.py`（341 行），公开 Interface 是 grant `list/create/update`、自然语言 proposal 的 registered Cluster+Environment 预门禁、model output 持久化/披露前的逐 target exact gate，以及 transaction-scoped `matching_ids_in/targets_authorized_in`。预门禁只确认当前 User 至少有一个与 Incident Cluster+Environment 相交的显式 Authority，因为 exact target 只能由 model 从自然语言中解析；Object/Namespace/Service/Cluster 的精确覆盖在 draft、diff read、Approval 与 start check 重验。它只通过 User、Connector Enrollment 和 Resource Catalog owner 的 `user_active_in`、`cluster_environment_in`、`service_active_in`、`service_bound_to_cluster_in`、`service_covers_target_in` 窄 Interface 读取外部状态，Platform Administrator/Team Membership 不进入 Authority 判定。

Phase Approval owner 为 `apps/aiops_k8s_gateway/kubernetes_phase_approvals.py`（584 行），公开 Interface `review/access_for_projection/approve/authorize_start/record_denial/audit_history` 冻结 ordered canonical Changes、dry-run diff/hash、risk、structured post-check、rollback policy、Authority IDs、actor/reason/request ID 与 10m/15m expiry。`authorize_start(phase_id, request_id, stage)` 按 exact Phase+revision 重读 Environment、Authority、Approval 和 expiry，并对成功/失败的 `grant|dispatch` check 分别写 immutable audit；K04 必须在 Execution Grant 签发和 execution dispatch 两处独立调用，不得复用先前结果。Phase 状态与 Change Request event 由 `change_plan_phases.py`（141 行）的 transaction Interface 管理；validation result 通过 `kubernetes_change_validation.py` owner 读取。Migration 22-25 分别拥有 approval status、Authority、Approval/audit 和非级联 rejected-proposal ledger；initial model scope expansion 被拒绝时删除不可 replay 的 orphan，但长期保留 request text、actor、idempotency/change-request/API request ID、result、reason 与时间，后续 rejection 则追加带 request ID 的 immutable event。

体量门禁：本票涉及的 >500 行文件及职责为 `change_requests.py`（799，Change Request/immutable revision、Authority-gated projection/rejection ledger）、`main.py`（729，仅装配与路由）、`connector_enrollments.py`（744，仅增加 Cluster Environment lookup）、`resource_catalog.py`（531，仅增加 Service active/Cluster binding/exact target lookup）、`v1_store.py`（660，仅增加 active User lookup）、`tests/test_gateway_kubernetes_phase_approvals.py`（559，Authority/Approval Module selector）；均未超过 800，新生产文件均低于 800。定向 selector 为 `tests/test_gateway_kubernetes_phase_approvals.py`、`tests/test_gateway_v1_kubernetes_phase_approvals_contract.py`、`tests/test_gateway_v1_change_requests_contract.py`、`tests/test_gateway_kubernetes_change_validation.py`、`tests/test_gateway_v1_auth_contract.py`、Gateway auth/incident/resource catalog/connector 直接消费者，以及 Console `src/api/client.test.ts`、`src/changes/change-requests-section.test.tsx`。最终核心 15 passed、直接消费者 22 passed、Console Vitest 11 passed、TypeScript/Vite production build 通过、全量 pytest 521 passed/2 skipped；Standards 与 Spec 双轴复审均零发现。

## K04 执行单个 Generic Kubernetes Change

**What to build:** Connector 使用 60s single-use Execution Grant 执行一个 approved create/patch/delete，重验全部 precondition/hash，并以 frozen structured post-check 得出可信 outcome。

**Blocked by:** K03 审批 Exact Change Plan Phase; P02 交付单入口 Canonical Kustomize Overlay.

- [x] Release 只给 Connector 专用 wildcard read/create/patch/delete ClusterRole，明确排除 update/deletecollection/impersonate/bind/escalate。
- [x] Connector claim 前校验 grant、change hash、UID/resourceVersion/old-value test；drift 返回 stale 且零 mutation。
- [x] started execution 默认 5m、最大 30m；later grant 只在 prior trustworthy terminal/post-check 后签发。
- [x] Connector journal 先 durable started/result 后 handoff；重复 command/grant 不重复 mutation。
- [x] Gateway/Connector/Console 端到端覆盖 success、API rejection、stale、post-check failure 和 Kubernetes audit identity。

体量门禁（K04）：Gateway Execution owner 为 `apps/aiops_k8s_gateway/kubernetes_change_executions.py`，公开 Interface 是 `start/dispatch_next/for_phase/record_started_in/record_result_in`；它独立拥有 60s single-use grant、300-1800s started deadline、request idempotency、Phase/Execution projection、Unknown Outcome reconciliation 与 audit。`change_requests.py`（800 行）只消费 Phase 的 effective status，公开 Interface 仍为 `get/list_for_incident/project_for_actor`；`main.py`（748 行）只增加 execution HTTP/Connector transport 装配与 transaction result fan-out；`connector_commands.py`（781 行）只通过 `poll/start/submit_result/reconcile_unknown_outcomes` 的窄 hook 承载 command lease/start/result transport；`connector_enrollments.py`（762 行）只新增 `execution_connector_in` 读取 current verified execute-capable Enrollment；`kubernetes_phase_approvals.py`（595 行）只由既有 `authorize_start` 在 grant/dispatch 重验 exact Authority，并允许 execution terminal/unknown status 的 Authority-scoped read projection。

Connector 的 `command_worker.py`（614 行）仍拥有 durable journal 与 `run_command_cycle`，新增 generic grant+command 去重和 structured execution handoff；`kubernetes_change_adapter.py`（579 行）仍是唯一 direct Dynamic Kubernetes API Adapter，公开 Interface `execute_validation_command/execute_change_command` 共享 exact discovery 约束，后者重验 hash/identity/resourceVersion/完整 old-value tests、执行 create/patch/delete，并在 started deadline 内轮询 frozen Kubernetes post-check。定向 selector 为 `tests/test_gateway_kubernetes_change_executions.py`、`tests/test_connector_kubernetes_change_execution.py`、Gateway K03 Approval/Connector/Auth contracts、`tests/test_connector_command_worker.py`、`tests/test_connector_kubernetes_change_adapter.py`、`tests/test_pilot_release.py`，以及 Console `src/api/client.test.ts`、`src/changes/change-requests-section.test.tsx`；所有涉及文件均不超过 800 行。

验收（K04）：Gateway/Connector 核心与直接消费者定向测试通过；Console Vitest 17 passed，TypeScript/Vite production build 通过；全量 pytest 549 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。

## K05 顺序执行 Multi-object Plan 并按 Frozen Inverse 回滚

**What to build:** 一个 approved Phase 可以顺序执行多个 Kubernetes Changes，首个失败即停止；已完成 step 只按 Approval 冻结的 inverse change 逆序 rollback，不声称跨对象原子性。

**Blocked by:** K04 执行单个 Generic Kubernetes Change.

- [x] 每 step 独立 grant/journal/result/post-check，后续 step 不会提前领取。
- [x] failed/stale/unknown step 停止 Phase；rollback 只在 frozen condition 成立时执行 exact inverse。
- [x] API surface-changing dependency 拆成新 Phase，重新 discovery/dry-run 并单独审批。
- [x] start 前 cancel 撤销 grant；start 后 `cancel_requested` 只停止后续 grant并等待当前 outcome。
- [x] `stop_only` 与 `rollback_completed` policy、rollback failure 和 audit timeline 有端到端测试。

迁移门禁（K05-1）：先以行为不变迁移把 K04 单步状态规范化为 Plan owner `kubernetes_change_executions.py`（520 行）与 `kubernetes_change_execution_steps`；公开 Interface 仍为 `start/dispatch_next/for_phase/record_started_in/record_result_in`，现有 HTTP/Connector contract 不变。Migration 27 只给 Phase 增加 nullable orchestration projection，Migration 28 将既有 execution/command/change/grant 无损迁入 ordinal 1 forward Step，并重建 Connector Command/validation 外键；`change_requests.py` 保持 800 行且只读取 effective Phase status，`v1_store.py`（661 行）只在测试清理入口先确保既有 identity schema 外键 owner。定向 selector 为 `tests/test_gateway_kubernetes_change_executions.py`、Gateway Approval/Connector/Auth contracts、`tests/test_gateway_kubernetes_change_validation.py` 与 `tests/test_gateway_connector_commands.py`；迁移后原 K04 公开行为保持通过。后续多 step、rollback、cancel 行为另行提交。

体量门禁（K05-2）：Plan execution Module 的公开 Interface 扩为 `start/cancel/dispatch_next/for_phase/record_started_in/record_result_in`；`kubernetes_change_executions.py`（728 行）只保留 Plan transaction 编排与 result progression，later-step grant、durable cancel、Frozen Inverse/projection 和 canonical hash 约束分别由同一 Module 内的 `kubernetes_execution_grants.py`、`kubernetes_execution_cancellation.py`、`kubernetes_execution_progress.py`、`kubernetes_inverse_changes.py` 与 `kubernetes_execution_codec.py` 内聚承载。`change_plan_phases.py` 继续唯一拥有 Phase orchestration projection 与 immutable event；`kubernetes_phase_approvals.py`（640 行）只扩展 active execution Authority recheck、cancel authorization 和 inverse freeze；Connector `kubernetes_change_adapter.py`（599 行）只扩展 trustworthy target identity result。所有文件均未超过 800 行。定向 selector 为 `tests/test_gateway_kubernetes_plan_execution.py`、`tests/test_gateway_kubernetes_inverse_changes.py`、既有 K04 execution/Approval/validation/Auth/Connector contract tests、`tests/test_connector_kubernetes_change_execution.py`、`tests/test_connector_kubernetes_change_adapter.py`、`tests/test_connector_command_worker.py`、`tests/test_pilot_release.py`，以及 Console API/Change Request tests。

验收（K05）：Gateway/Connector/contract 定向测试 89 passed/2 skipped，review 修正后的核心 selector 56 passed；Console Vitest 19 passed，TypeScript/Vite production build 通过；全量 pytest 569 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。

## K06 通过 Secure Input 执行 Sensitive 或 Irreversible Change

**What to build:** User 可以在不把 plaintext 交给模型、API response、diff 或 audit 的情况下提供 sensitive value；Cluster Change Authority 可以明确审批无法可靠 rollback 的 change。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态; K04 执行单个 Generic Kubernetes Change.

**体量门禁（2026-07-13）：** Secure Input 归属独立 Gateway Module，公开 Interface 为创建/投影、为 validation/execution 解析 encrypted refs、key availability 检查与 terminal cleanup；不可逆确认归属 Phase Approval Interface。定向 selector 为 `tests/test_gateway_secure_inputs.py`、`tests/test_gateway_kubernetes_change_validation.py`、`tests/test_gateway_kubernetes_phase_approvals.py`、`tests/test_gateway_kubernetes_change_executions.py`、`tests/test_connector_kubernetes_change_adapter.py`、`tests/test_connector_kubernetes_change_execution.py`、`tests/test_gateway_v1_secure_inputs_contract.py`、`tests/test_gateway_v1_kubernetes_phase_approvals_contract.py` 和 Console change section tests。需接线的既有大文件为 `kubernetes_phase_approvals.py`（640 行）、`kubernetes_change_executions.py`（728 行）、`command_worker.py`（614 行）、`kubernetes_change_adapter.py`（599 行）与进程入口 `main.py`（748 行）；它们分别保持 Approval、execution orchestration、Connector journal/worker、Kubernetes Adapter 与装配职责，不在其中新增加密存储实现。`test_gateway_kubernetes_phase_approvals.py`（582 行）只补充该 Interface 的安全回归，不拆分测试 owner。

迁移门禁（K06-1）：任务开始时 `change_requests.py` 为 800 行，所属 Change Request Module 的公开 Interface 为 `submit/get/list_for_incident/project_for_actor/record_validation_result_in`。行为不变的 read projection 已完整迁入 `change_request_projection.py` 并以 `17c90e2` 独立提交；隔离 worktree 中 `tests/test_gateway_v1_change_requests_contract.py`、Kubernetes validation/Approval selectors 共 14 passed。K06 行为提交只在 projection Module 接入 `availability_status` 与 Validation 的公开 projection Interface，`change_requests.py` 保持 744 行且不新增领域决策。

体量门禁（K06-2）：Secure Input Module 公开 Interface 扩为 actor-scoped create/get、按 ID 生成 public/execution refs、revision hold/release、key availability 与 ref-aware cleanup；`secure_input_transport.py` 和 `secure_input_execution.py` 分别拥有 transport 终态脱敏与 execution availability transition。最终 `kubernetes_change_executions.py` 800 行、`kubernetes_phase_approvals.py` 690 行、`command_worker.py` 651 行、`kubernetes_change_adapter.py` 689 行、`main.py` 764 行，均未超过 800 行；共享 `{key_name, sha256}` 约束由 `aiops.security.public_secure_input_facts` 统一。定向 selector 另覆盖 `tests/test_connector_command_worker.py`、`tests/test_gateway_kubernetes_sensitive_authority.py` 和 `tests/test_gateway_v1_auth_contract.py`。

- [x] Secure Input 独立于 Change Request，模型只见 opaque placeholder；Gateway 使用 CSPRNG 或 User input。
- [x] Gateway/Connector 以独立 change encryption key 保存 ciphertext/nonce/hash，plaintext 只在内存中短暂存在。
- [x] Approval/diff/report 只显示 key name/hash；terminal rollback window 后删除 ciphertext。
- [x] key loss 使未完成 plan `secure_input_unavailable`，不重新生成 credential或执行 placeholder。
- [x] Irreversible Change 显示 concrete loss、`rollback: unavailable`，要求 fresh auth/reason/重新输入 exact target。
- [x] Secret redaction、key rotation/loss、cluster authority 和 irreversible confirmation 有安全测试。

验收（K06）：Gateway/Connector/Secure Input/Approval/contract 定向测试 106 passed/2 skipped，最终 review 缺口的 revision hold、普通 validation failure、Connector start 拒绝与 privileged workload Authority selectors 17 passed；Console Vitest 21 passed，TypeScript no-emit 与 Vite production build通过；全量 pytest 597 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。Connector 在 Gateway 未确认 start（含 supersede race）时立即将 accepted transport 重写为 key name/hash，后续合法 re-lease 仍可重新接收受控 ciphertext，失败路径不遗留 nonce/ciphertext。

## K07 保守 Reconcile Unknown Outcome

**What to build:** response loss 或 Connector restart 后，系统区分 confirmed result、Observed Effect 和 Unknown Outcome，暂停后续执行且绝不自动 retry。

**Blocked by:** K05 顺序执行 Multi-object Plan 并按 Frozen Inverse 回滚; K06 通过 Secure Input 执行 Sensitive 或 Irreversible Change.

**体量门禁（2026-07-13）：** K07 Reconciliation 归属 Gateway 独立 Module，公开 Interface 为记录 Unknown Outcome、接收只读 observation、投影 immutable evidence 与接受 User reconciliation；Connector 只提供 durable journal terminal evidence 和 Kubernetes read/post-check Adapter。任务开始时 `kubernetes_change_executions.py` 800 行，公开 Interface 为 `start/cancel/dispatch_next/for_phase/record_started_in/record_result_in`；`connector_commands.py` 781 行，公开 Interface 为 `queue_read/poll/start/submit_result/get`；`command_worker.py` 651 行、`kubernetes_change_adapter.py` 689 行、`main.py` 764 行。先以独立提交把 execution transport timeout reconciliation 与 Connector terminal result acceptance 行为不变迁入所属 Module 内的窄子模块，selector 为 `tests/test_gateway_kubernetes_change_executions.py`、`tests/test_gateway_kubernetes_plan_execution.py`、`tests/test_gateway_connector_commands.py`、`tests/test_connector_command_worker.py` 和 V1 Connector contract；K07 行为另行提交，所有文件保持不超过 800 行。

- [x] 只有可信 Connector journal terminal result 可 confirmed succeeded/failed。
- [x] exact live state + post-check 但无 attribution 记录 Observed Effect；不匹配/模糊保持 Unknown Outcome。
- [x] 两者暂停 Phase，User 接受 reconciliation evidence 后模型才能基于 live state重新规划。
- [x] governance history 永久保留 redacted plan/diff/Approval/grant/outcome；Connector terminal journal 30d bounded cleanup。
- [x] restart recovery、late result、no retry、effect attribution 和 audit projection 有定向测试。

体量门禁（K07-2）：Gateway Reconciliation Module 为 `kubernetes_reconciliation.py`（595 行），公开 Interface 是创建 Unknown Outcome observation、接收安全 observation result、处理可信 late terminal、投影 evidence 与 actor-scoped acceptance；migration 34 只扩展 Connector Command action/journal timestamp 并重建直接外键 consumer，migration 35 增加独立 Phase reconciliation projection。Connector `kubernetes_reconciliation_adapter.py`（160 行）通过 Kubernetes Adapter 的公开 `observe_change_state/json_pointer_value` Interface 只读 exact target 与 frozen post-check，不保存 full live object；`command_worker.py`（737 行）只扩展 journal terminal evidence、read-like restart retry 与 reconcile dispatch。最终 `kubernetes_change_executions.py` 791 行、`connector_commands.py` 703 行、`main.py` 778 行、`kubernetes_change_adapter.py` 707 行，均不超过 800 行；`test_gateway_kubernetes_change_executions.py` 799 行且定向 selector 保持独立。

验收（K07）：Gateway/Connector/Kubernetes Change/HTTP contract 核心定向测试 63 passed/2 skipped；Console Vitest 23 passed，TypeScript no-emit 与 Vite production build 通过；全量 pytest 604 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。可信 journal evidence 对所有 mutation action 统一强制；read-like journal 支持 started 后 restart retry；Unknown Outcome 保留未执行步骤但不 dispatch，可信 late result 仅在 User 接受前恢复旧 Plan，接受后只补记 confirmed step outcome且不恢复旧 Plan。

## K08 迁移 Caller 并退役有限 Mutation Contract

**What to build:** Workbench、Report、Notification 和 verification 全部使用 Generic Change Request/Plan；完成迁移后删除旧 direct Recommended Action Approval 与 typed restart/scale/rollback product contract。

**Blocked by:** K07 保守 Reconcile Unknown Outcome.

**体量门禁（2026-07-13）：** K08 只迁移旧有限 Mutation contract 的直接 caller 并删除旧 owner。Evidence/Recommendation Module 的公开 Interface 保持 Diagnosis writeback、Workbench guidance projection 与 stale invalidation；`evidence_decisions.py`（551 行）删除可审批 typed action 决策，不拥有 Change Request 创建或执行。`toolsets/incident_diagnosis.py`（1515 行）的公开 Interface 是 evidence-grounded diagnosis/result rendering；其 Recommendation normalization/prompt/render 完整能力迁入同一领域 Module 的窄文件并删除旧实现，原文件结束时不得高于 1515 行。Change Request Module 的公开 Interface 仍为 submit/input/retry/projection/validation result；`change_requests.py`（744 行）只在 validation success transaction 投影 Phase-scoped Notification Request，不新增状态机。Incident Module 的公开 Interface 仍为 lifecycle/workbench；`incident.py`（750 行）只把 resolution blocker 从 legacy grant/command join 改为 Generic Phase execution terminal state，不新增执行决策。Gateway `connector_commands.py`（703 行）继续只拥有 read/validation/execution/reconciliation Command lifecycle，`kubernetes_reconciliation.py`（595 行）只在最终 schema migration 中移除 legacy grant/typed action 字段，`main.py`（778 行）只删除旧 Adapter 装配。Connector `command_worker.py`（737 行）保留 read、generic validation/execution/reconciliation dispatch 与 durable journal，`kubectl_executor.py`（639 行）收敛为 read-only argv Adapter；typed Deployment mutation Module 在同一变更删除。Connector Kubernetes Change Adapter 的公开 Interface `execute_validation_command` 在 live Deployment 缺少 annotations map 时把 reserved restart mutation canonicalize 为 parent/child RFC 6902 patch，`kubernetes_change_adapter.py` 从 707 行增至 724 行，定向 selector 为 `tests/test_connector_kubernetes_change_adapter.py` 与 `tests/test_k08_canonical_restart_flow.py`。其余定向 selector 为 `tests/test_incident_diagnosis.py`、`tests/test_gateway_diagnosis_delivery.py`、`tests/test_gateway_incidents.py`、`tests/test_gateway_v1_incident_contract.py`、replacement contract/legacy absence tests、`tests/test_gateway_incident_reports.py`、`tests/test_gateway_notification_requests.py`、`tests/test_connector_command_worker.py`、`tests/test_command_gateway_skeleton.py`、Generic Kubernetes Change execution/reconciliation contract tests，以及 Console Workbench/Admin/Report tests。500+ 行测试只通过公开 diagnosis、HTTP、owner Interface、Connector worker 和 kubectl Adapter seam 验证 replacement/absence，不断言内部调用顺序。

**体量门禁补充（K08 review）：** Incident Module 的公开 Interface 扩为 lifecycle/workbench/planning facts；`incident.py` 从 750 行增至 782 行，把 exact Recommendation summary 对应的受限 `change_intent` 投影到 sanitized planning facts，不新增执行决策。定向 selector 为 `tests/test_gateway_diagnosis_delivery.py` 与 `tests/test_gateway_v1_change_requests_contract.py`。

- [x] Workbench 从 Recommendation 创建 Change Request，不再直接批准并执行 action。
- [x] Report/Notification 投影新的 Plan/Phase/Approval/Execution/rollback/reconciliation history。
- [x] verification restart 使用 canonical RFC 6902 annotation patch 与 generic Connector execution。
- [x] 同一变更删除旧 public routes、OpenAPI schema、Gateway owner、Connector typed mutation branch 和对应 UI caller。
- [x] 不保留 wrapper、双写、双读或无退出条件 compatibility path。
- [x] replacement contract、legacy absence、architecture boundary 和直接消费者测试全部通过。

验收（K08）：Recommendation 保持 guidance-only，Workbench 只显式创建 Generic Change Request；Report 与 Notification 从 durable Generic Plan/Phase/Approval/Execution/rollback/reconciliation history 投影。Diagnosis Recommendation 的受限 `change_intent` 经 Gateway-owned planning facts 到达 strict canonical restart boundary；Connector 对缺失 annotations parent 的 Deployment 生成 parent/child RFC 6902 patch，execution 与 Unknown Outcome reconciliation 均复用 Generic Connector contract。历史 v9/v11-v13/v19/v34 migration 保持原行为，v36/v37 分别完成 Recommendation 与 typed mutation retirement；v13 legacy command、v35 legacy Recommendation 和 fresh schema 均有升级测试。K08 owner/consumer selector 283 passed/2 skipped（最终修正定向回归另 20 passed 与 9 passed），Console Vitest 29 passed，TypeScript no-emit 与 Vite production build 通过；全量 pytest 592 passed/2 skipped；Standards 与 Spec 最终复审均零 finding。仅保留既有 >500 kB bundle warning。

## C04 交付跨 Incident 的变更中心

**What to build:** 有权 User 可以从 `变更` 找到需要输入、审批、reconciliation 或查看 outcome 的 Change Request，并审阅 exact diff、执行历史和来源 Incident。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; K08 迁移 Caller 并退役有限 Mutation Contract.

迁移门禁（C04-1）：任务开始时 `apps/aiops_console_web/src/changes/change-requests-section.tsx` 为 523 行，所属 Change Request Console Module 的公开 Interface 为 Incident-scoped request 创建、blocking input/retry、exact diff 与 Gateway-owned governance commands，定向 selector 为 `apps/aiops_console_web/src/changes/change-requests-section.test.tsx`。行为不变迁移把 Phase Approval、execution、cancel 与 reconciliation acceptance 完整能力移入可复用的 `change-request-governance.tsx`（381 行），原文件降至 192 行且只保留 Incident-scoped request/draft UI；迁移后原 selector 11 passed、TypeScript no-emit 通过。后续 C04 页面复用该 Interface，不复制浏览器状态机。

体量门禁（C04-2）：Gateway `main.py` 任务开始时为 763 行，所属进程装配层的公开 Interface 仍为 HTTP route dispatch 与依赖装配；本票只装配 Change Center HTTP Adapter，结束时为 769 行，不新增领域决策。现有 `change_requests.py` 为 759 行且本票不增长，Change Request Module 的公开 Interface 保持 request/phase projection 与既有 governance commands。新增 Change Center read Module 通过 `ChangeCenter.list_for_actor/detail_for_actor` 投影跨 Incident actor-scoped summary/detail，HTTP Adapter 只负责认证、Incident scope 裁剪与序列化；Console Change Center Module 的公开 Interface 为 URL-owned filter、list/detail view 和既有 Change Request governance 复用。定向 selector 为 `tests/test_gateway_change_center.py`、`tests/test_gateway_v1_change_center_contract.py`、`apps/aiops_console_web/src/changes/change-center-page.test.tsx`、`apps/aiops_console_web/src/shell/console-shell.test.tsx` 与既有 `change-requests-section.test.tsx`；直接 contract 消费方 selector 覆盖 Change Request、Phase Approval、validation、execution、reconciliation 和 Incident workbench。

- [x] Change owner 提供 actor-scoped list/detail projection，覆盖 active、paused 和 terminal phase/outcome。
- [x] `/changes` 默认突出当前 User 可处理项；status/Environment filter 由 URL 拥有。
- [x] detail 展示 Evidence refs、target、dry-run diff、risk、Approval、Execution、rollback/reconciliation history。
- [x] approve/cancel/accept reconciliation 调 owner command，不在 Console 建状态机。
- [x] Authority non-disclosure、pending count、OpenAPI、stale/unknown 和窄屏 diff 有定向测试。
- [x] 页面可用后才把 `变更` 加入共享导航。

验收（C04）：Change Center owner 从 actor-visible Incident scope 投影 list/detail，复用 Change Request authority redaction，并仅把当前 Actor 可调用 input/retry/approval/execution/reconciliation command 的状态计入 pending；executing/terminal projection 保留 authorized frozen Phase review，无 Authority 时继续 fail closed。detail 从 Incident owner response 投影 Evidence references，展示 active exact diff、frozen approval target/diff/risk、Plan Revision 与 durable governance event history，并复用既有 approval/execution/cancel/reconciliation command UI；所有 mutation 同时刷新 Incident 与 Change Center cache。`/changes` 的 status/Environment filter 由 URL 拥有，desktop/mobile 共享导航只在定向验收后接入。Change Center 与直接 Change Request/Approval/validation/execution/reconciliation/Incident contract selector 67 passed，终态补充回归 9 passed；Console Vitest 32 passed，TypeScript no-emit 与 Vite production build 通过；全量 pytest 599 passed/2 skipped；Standards 与 Spec 双轴最终复审均零 finding。仅保留既有 >500 kB bundle warning。

## S04 发布 Optional Web Setup 与真实 Platform Status

**What to build:** 所有 User 可以持续查看四项真实 capability status；Platform Administrator 可以配置、验证、skip optional capability并恢复失败，Incident workspace 始终可进入。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; S01 完成多 Connector Enrollment 与 Read Verification; S02 配置并验证真实 Model Provider; S03 把 Notification Test Delivery 绑定到 Revision Readiness; O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics; O02 部署真实 Loki 与 Alloy 日志链路.

体量门禁（S04）：`apps/aiops_k8s_gateway/main.py` 任务开始时 785 行，所属 Gateway 进程装配层的公开 Interface 仍为 HTTP route dispatch 与依赖装配；本票只增加 Platform Status HTTP Adapter 的 import/dispatch 和 Notification 配置保存后的 setup-decision owner 装配，当前 788 行，不新增领域决策且保持低于 800 行。新增 Gateway Platform Status Module 的公开 Interface 为四 owner capability snapshot、Notification setup decision 与配置保存恢复 active；`platform_status.py` 当前 666 行、公开 Interface 测试 `test_platform_status.py` 当前 751 行，定向 selector 为该文件和 `tests/test_gateway_v1_platform_status_contract.py`。Console Platform Status Module 的公开 Interface 为 `/platform` capability rail、真实 owner 验证/重试、Notification skip/resume 与安全只读投影；定向 selector 为 `apps/aiops_console_web/src/platform/platform-status-page.test.tsx`、`apps/aiops_console_web/src/shell/console-shell.test.tsx` 和 `apps/aiops_console_web/e2e/platform-status.spec.ts`。

验收（S04）：Gateway 实时并行聚合 Model、Notification、Connector 与 Prometheus/Loki owner 状态，单 owner 3 秒超时只降级自身，所有 owner response 均有 64 KiB 上限；Connector 投影 bounded connection state/计数，只有当前 online Enrollment 与 online/verified Cluster 配对才产生 ready。Notification skip 持久记录 actor/reason/time/request ID、禁止 ready/required capability skip、配置保存自动恢复 active；显式 decision 与 configuration auto-resume 均按 request ID 幂等重放且不回滚较新状态。真实 rollout 暴露的旧 v39 表缺 `expected_revision` 由 v40 修复，v41 区分 legacy mutation identity 并持久化 configuration resume operation；v42 将既有 v40 非空 `expected_revision` operation 标记为新 identity，并从成功 Notification destination audit 回填 configuration resume operation，保留旧 v39/v40 历史升级回归。`/platform` 使用 07 capability rail，ready Notification 不展示 skip；普通 User 只读，管理员调用真实 owner test/retry 与 `/admin` 详细管理，prototype fake state 已删除。S04 owner/direct/manifest selector 86 passed/2 skipped，Console 48 passed，Playwright desktop 1440×900 与 mobile 390×844 顶层导航 re-entry 共 4 passed，TypeScript/Vite build 通过；全量 pytest 687 passed/4 skipped。公开 canonical digest 为 Gateway `8d587b3c…72fada`、Console `fae0e2c9…a186b`、Diagnosis `8735b861…c62d2`、Notification `19668e8a…fb919`，所有 Deployment 均 1/1 Ready，bootstrap Job 使用同 Gateway digest 完成。NodePort 验收确认 migration 42、Observability `ready`、Connector offline/verification 如实 bounded 投影、Notification skip 跨重新登录持久且最终恢复 active，Incident API 与 `/platform` 页面均为 200；Console 加载最终 lazy chunk `platform-status-page-DYPMptBp.js`。此前 S04 rollout 的 owner scale-to-zero 验收同样通过。Standards 与 Spec 双轴最终复审均零代码 finding，复审指出的 stale evidence 与 immutable artifact 缺口已由本次记录和 rollout 关闭。

- [x] Gateway 聚合 owner response，不复制 configuration、不持久化全局 `setup_complete/all_ready`。
- [x] capability 分别投影 readiness/configuration/revision/verification/availability/safe reason，owner timeout 不拖垮其他项。
- [x] skip 记录 actor/reason/time且永不算 ready；保存新配置自动恢复 active。
- [x] `/platform` 使用 07 选定 capability rail；admin 有 configure/test/retry，普通 User 只读安全摘要。
- [x] `/admin` 保留详细领域配置；删除 prototype variant/scenario 和 in-memory fake state。
- [x] desktop/390px、re-entry、partial owner failure、fresh-auth 和 secret non-disclosure 有端到端测试。

## V01 交付 Version-matched Controlled Verification Fixture

**What to build:** Operator 可以显式安装一个隔离 verification workload，并以 Kubernetes 生成 run ID 触发不会被 liveness 自动修复的真实 readiness fault；cleanup 不触碰产品治理历史。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay; O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics; O02 部署真实 Loki 与 Alloy 日志链路; K08 迁移 Caller 并退役有限 Mutation Contract.

体量门禁（V01）：`tests/test_pilot_observability_integration.py` 任务开始时 525 行，所属 Observability 真实 Cluster Integration Test Module 的公开 Interface 为 `AIOPS_RUN_KUBERNETES_INTEGRATION=1` opt-in selector 下的 Prometheus/Alertmanager/Gateway/MCP 与 Alloy/Loki owner-path 验收。本票只把其中旧不可调度 Deployment fixture 段迁到 version-matched `verification/base`、`verification/run` 与 annotation rollout seam，最终 657 行，保留同文件的真实边界职责并低于 800 行；定向 selector 为该文件、新增 fixture app/manifest selector 和 `tests/test_pilot_observability.py`。

- [x] optional `base` 创建固定 namespace、quota/limit、restricted security、NetworkPolicy、ServiceAccount、Deployment/Service。
- [x] app 提供 live/ready/metrics/internal trigger；fault 使用 Job controller UID 幂等 latch，真实 stdout log 与 metric 带 bounded run ID。
- [x] `run` 只创建 trigger Job；重复 Pod request 幂等，不同 run 在 active fault 时 conflict。
- [x] Prometheus rule exact 匹配 fixture 并携带 correlation labels；恢复使用 approved pod-template annotation rollout。
- [x] immutable image、resource ceiling、RBAC/network isolation、rerun 和 delete overlays 有定向集成测试。

验收（V01）：默认 Pilot 不引用 `verification/`；Operator 显式 apply `verification/base` 与 `verification/run`。base 固定 `aiops-verification` namespace、5 Pod/1 Deployment/1 Job 与 CPU/memory 总 ceiling、restricted Pod Security、无 token ServiceAccount、default-deny NetworkPolicy、单副本 Deployment/ClusterIP Service；trigger Job 通过 Kubernetes 注入的 Service env 定位 app，egress 只允许 matching app 8080/8081，Prometheus 只可访问 metrics 9090。`FaultLatch` 对 bounded Kubernetes Job controller UID 首次 activate、同 UID replay、异 UID active conflict；Service 在 NotReady 时仍发布 Endpoint，使同 UID retry 可到达 latch；fault 只令 readiness 503，liveness 200，stdout 与 `aiops_verification_fault_active` 均携带 run ID。Kubernetes 1.26 使用同一 controller UID 的 legacy label，最低版本保证新版 label 后有明确删除条件。Prometheus 从 Pod label 投影 `deployment` 并由 rule exact 匹配 namespace/deployment/service/metric，显式携带 cluster/namespace/deployment/service/run ID correlation labels；恢复 annotation seam 为 `aiops.dev/verification-run-id`。fixture 镜像由现有 split-image CI target 发布，公开 canonical digest 为 `8807dae0…bfdb0`，base/run 使用同一 digest且 non-root/read-only/no kubectl。

V01 app/manifest/observability/packaging selector 65 passed/2 skipped；最终 digest 的真实 Cluster selector 1 passed in 108.57s，证明两次不同 Job UID、Alloy/Loki activation/recovery log、Prometheus firing/resolved、Alertmanager→Gateway Incident、annotation rollout seam 与 rerun；直接 O02 owner-path 1 passed in 98.29s。live fixture selector 中的 direct patch 仅验证 workload mechanics，不计为 governed Approval 证据；既有 K08 Approval/Grant/Connector contract selector 50 passed，真正 live governed recovery 与两轮治理历史分别由后续 A02/A04 frontier 验收。cleanup 后 `aiops-verification` namespace 消失，12 个产品 Deployment 均 1/1，Incident workbench 仍可经公开 API 读取。全量 pytest 696 passed/4 skipped；Standards 与 Spec 最终复审均零 finding。

## P03 打包最终 Immutable Pilot Release

**What to build:** Release Operator 可以发布并验证一个 self-contained `aiops-pilot-vX.Y.Z.tar.gz` 与 `SHA256SUMS`，新 Operator 解压后无需仓库或构建工具即可安装完整 candidate。

**Blocked by:** C02 交付有权限范围的资源工作区; C03 交付 Incident Report 资料库; C04 交付跨 Incident 的变更中心; S04 发布 Optional Web Setup 与真实 Platform Status; V01 交付 Version-matched Controlled Verification Fixture.

- [x] archive 顶层就是唯一 Kustomize overlay，包含本地 manifest、verification overlays 和简短安装/恢复说明。
- [x] 所有 workload image 为完整公开可拉取 digest；无 remote base、zero digest、mutable tag 或 source checkout dependency。
- [x] Console/Gateway OpenAPI producer/consumer、owner image/config revision 和 release version一致。
- [x] package test 从空目录校验 checksum、render、image inventory 和禁止路径。
- [x] 当前只承诺 clean install/same-version reapply，不暗示 upgrade/downgrade/backup/uninstall。

验收（P03）：`scripts/build_pilot_release.py` 将 canonical Pilot render 成归档内单一本地 `manifest.yaml`，顶层 overlay 只引用该文件，并按显式 allowlist 附带 version-matched verification base/run、中文安装/恢复说明、Gateway OpenAPI/Console consumer 快照与 `release.json`。release `v0.1.0` 强制匹配 Console package `0.1.0`；所有资源与 workload template 带同一 release annotation，metadata 固定 owner image inventory、ConfigMap data revision 与 contract hash。builder 对每个 Kustomize resource 要求 resolve 后存在且不能逃出 release root；对全部 product/fixture digest 使用空 Docker config 做匿名 manifest inspect，直连 `registry.k8s.io` 确认超时后仅该 registry 使用用户授权代理，其余 registry 保持直连，最终全部通过。

实际发布物为 `dist/aiops-pilot-v0.1.0.tar.gz`（41 KiB）与 `dist/SHA256SUMS`。A01 runner 暴露 Platform Status fresh-auth/SSE 与 Notification durable attempt ID 产品缺口后，已用公开可匿名拉取的 Gateway `16c3d1d4…10f7fbd`、Notification `31737782…f4c6e` digest 重建当前 candidate，archive SHA256 为 `fc1b649d32d74c1037dde99b7065f29f0a615d14b1cb7fd577b01a3f9822c11a`；此前 `59880499…42a` artifact 已被 supersede，不得用于 A01。空目录测试验证 safe extraction、checksum、确定性 rebuild、唯一顶层 overlay、本地 render、完整 file/image inventory、zero/tag/Operator CRD 禁止、verification dry-run 与 OpenAPI 全量重新生成逐字节一致；原 package selector 7 passed，Pilot/verification/contract 直接消费者合计 68 passed，A01 candidate 重建后 package/verification selector 42 passed，TypeScript/Vite production build 通过，仅保留既有 >500 kB warning；A01 runner 的 P01/P02 对新 SHA 均 passed。归档与 metadata 只声明 clean install/same-version reapply，并明确排除 upgrade、downgrade、backup、rollback 与 data-preserving uninstall；Standards 与 Spec 最终复审均零 finding。

## A01 自动执行 Package Install 与 Setup Gates

**What to build:** Release verifier 可以为一个 clean non-production Cluster 创建脱敏 acceptance evidence bundle，自动执行 package、install、HTTP access、first-login security 和 setup readiness gates，并由人员完成必要 attestation。

**Blocked by:** P03 打包最终 Immutable Pilot Release.

实现进度（A01 runner，尚未完成 live acceptance）：Acceptance Evidence Module 的公开 Interface 为 immutable candidate/run identity、frontier/append-only gate attempt、manifest/artifact revalidation、脱敏 artifact/hash、OpenSSH-signed human attestation 与最终 `SHA256SUMS`；Package/Cluster/Web/Platform Status/Model/Notification/Connector/Observability runner 只编排 subprocess、Kubernetes、Gateway HTTP、真实 Playwright 和 evidence，不写产品 DB 或第二套 acceptance state。HTTP client 显式禁用环境代理，浏览器使用 `--no-proxy-server`，通用 subprocess Adapter 剥离 ambient proxy environment；外部 credential 只经内存/getpass/子进程 stdin 流转。`P01-P03`、`I01-I05`、`S01-S06` policy、clean preflight 32Gi PVC/全节点 digest pull、same-version reapply Secret hash、role/CSRF/fresh-auth、invalid→real provider、dead-letter→sent/selected、release-bound Connector identity/Secret/rollout/read verification 和真实 `aiops_*` telemetry/MCP Adapter 均有定向测试。`evidence.py`（589 行）的单一职责是 fail-closed acceptance ledger，公开 Interface 为 create/open/start/record/attest/finalize；`cluster_install.py`（798 行）的单一职责是 clean Cluster preflight 与同一 release install/reapply Kubernetes gate，公开 Interface 为 `run_p03/run_i01/run_i02`；二者低于 800 行且各自通过公开 Interface 测试，不为行数拆出转发层。Model、Notification、Connector、Observability 与 Platform Status gate 已按独立变化能力拆为各自低于 500 行的 Module。Notification Delivery owner `notification_service/requests.py` 为 783 行，公开 Delivery history Interface 只补回 durable attempt ID 以满足既定 retained evidence contract，保持低于 800 行；定向 selector 为 `tests/test_notification_service.py` 与 `tests/test_gateway_v1_notification_contract.py`。Gateway `main.py` 仍为 788 行，A01 只把现有 Platform Status Adapter 装配到既有 fresh-auth guard；领域判断留在 Adapter。A01 定向 selector 为 `tests/test_pilot_acceptance_*.py`、`tests/test_gateway_v1_platform_status_contract.py`、`tests/test_platform_status.py` 及前述 Notification selectors。真实通过状态仍必须由新 immutable Gateway digest 重建 candidate，并在新的 clean non-production Cluster 上使用 Model/Notification credential、ordinary User 和人员签名完成；当前已安装 Cluster 不可冒充。

实现验收（A01 runner）：最终定向 selector 73 passed；真实 P01/P02 runner 均 passed，其中 P02 当前源代码 contract 82 passed、Console 17 passed且 production build 通过；全量 pytest 735 passed/4 skipped；Standards 与 Spec 双轴最终复审均零 finding。Platform Status authenticated SSE 复用真实 owner snapshot，使 clean install 无需 seed Incident 即可完成 I03 handshake。以上只证明 runner/product contract，未把旧 artifact 或当前已安装 Cluster 记作 live A01 通过。

当前 live candidate（A01）：`dist/aiops-pilot-v0.1.0.tar.gz` SHA256 `43ca9d4a64772c4db33a732da64769b436fb77e8df87413741b2879e66313935`；Gateway `sha256:16c3d1d4f112d6fbb7fa6eb89736a9a8dd4b29d2f9c53e6f1b057089f8107fbd` 与 Notification `sha256:317377822411c33ef95dd1e838d1e02909b5aa260a740ab85e351f83321f4c6e` 均已通过 split-image import smoke、推送和空 Docker config 匿名 manifest inspect。直连 `registry.k8s.io` 的 kube-state-metrics manifest 首次 60s 超时后，仅该目标使用用户授权代理完成 rebuild，其余 registry 保持直连；代理未写入项目配置。

Live 进度（2026-07-15）：用户授权清空既有 Pilot 后，canonical overlay 已删除 `aiops-system` 与全部 release Cluster RBAC，`aiops-verification` 原本不存在；NodePort 30088 空闲，默认 `local-path` StorageClass 与两节点 Ready Calico 已核实。首个正式 evidence `/root/aiops/acceptance/v0.1.0-20260714T235407Z` 绑定当前 kube context/cluster identity 与上述 candidate SHA，P01/P02 连续 passed；其后 P03 attestation 与失败 attempt 见下条记录。

P03 首次 live attempt（失败证据保留）：用户确认 non-production/可自由配置后，以 dedicated OpenSSH key 完成 Platform Operator attestation；32Gi PVC 已 Bound，但 image probe 对默认 root image 只设 `runAsNonRoot`、未设 UID，导致 `CreateContainerConfigError` 并使全节点 digest matrix 无法收敛。该失败 run 永不改写为 passed。Runner 已在既有 probe securityContext 固定 UID/GID 65532，并以 structured node/image/reason 覆盖未调度、waiting、terminated、running 与缺失 node/image Pod，且不把已有 imageID 的 pair 误报为失败；`tests/test_pilot_acceptance_cluster.py` 6 passed，全部 A01 runner selector 35 passed。修复只影响本地 acceptance runner，不改变 release artifact；验证修复后须以新 acceptance_id 从 clean P01 重来。

后续 live attempts（失败证据保留）：第二个 clean run `/root/aiops/acceptance/v0.1.0-20260715T011422Z` 的 P01/P02 passed，P03 精确收敛为两个 node 仅 `registry.k8s.io/kube-state-metrics` `ImagePullBackOff`；按用户授权在两节点 containerd systemd drop-in 中仅令 registry.k8s.io 走 `10.0.41.206:30789`，Aliyun/Quay/Cluster 网段保持 `NO_PROXY`，临时 `imagePullPolicy: Always` DaemonSet 已证明两节点得到 exact digest，配置 DaemonSet 随后删除。第三个 clean run `/root/aiops/acceptance/v0.1.0-20260715T012530Z` 的 P01-P03/I01 passed，I02 因 `kubectl rollout status daemonset --all` 不受 kubectl v1.26 支持而 failed；native `kubectl rollout status daemonset -n aiops-system` 已在真实 release DaemonSet 成功，runner 删除无效 `--all` 并以公开 I01/I02 Interface 回归覆盖。该 run 不改写，修复后再次 clean restart。

最新失败 run（证据保留）：`/root/aiops/acceptance/v0.1.0-20260715T013212Z` 从 clean Cluster 连续完成 P01-P03/I01-I05，I05 使用人员签名、真实 NodePort 浏览器首次登录及通过 Console 创建并登录的 ordinary User；S01 随后发现 setup decision 返回 200，但审计无法按调用方 `X-Request-ID` 关联并记为 failed。根因是 canonical Nginx edge 以 `$request_id` 覆盖客户端关联 ID；修复将 Host、X-Forwarded-For 与客户端 X-Request-ID 统一放在 server scope，使 `/api/v1` 与 `/auth` 继承同一代理头约束。rendered overlay 与 Platform Status 直接消费者 34 passed，双轴复审零 finding，真实 NodePort probe 证明响应和 admin audit 均保留 exact request ID。失败 run 不再继续；新 candidate 必须从 clean P01 重来。

- [ ] runner 只组织 commands/evidence，不写产品 DB、不 seed state、不保存 secret，并为每 gate 记录 pass/fail/artifact hash。
- [ ] package/preflight/install/reapply/NodePort/same-origin/login/CSRF/role checks 对齐 08 的 `P/I` gates。
- [ ] Model invalid->verified、Notification dead-letter->sent、Connector read verified、真实 telemetry 对齐 `S` gates。
- [ ] HTTP NodePort mandatory；HTTPS Ingress 只有声明该 profile 时 conditional gate。
- [ ] 失败 attempt 不覆盖，修复 candidate 后必须从 clean run 重来；HITL receipt/login 有签名 attestation。

## A02 完成第一轮真实 Alert-to-Report

**What to build:** 新 Operator/SRE 从公开产品边界完成第一轮 fault、Incident、真实 model/evidence、Change、Approval、rollout recovery、Report v1 和真实 resolved Notification，并生成可关联 evidence index。

**Blocked by:** A01 自动执行 Package Install 与 Setup Gates.

- [ ] 只通过 Console 建 Team/Service/Binding/Authority，通过 verification overlay 触发真实 Alert。
- [ ] Prometheus/Loki/Connector 四类 fresh Evidence 经 MCP/owner path 满足 deterministic gate。
- [ ] User 创建 Change Request，模型生成 annotation patch，API Server dry-run 和 namespace Authority Approval 后只执行一次。
- [ ] resolved webhook/stabilization、immutable Report v1、outbox->Notification Delivery `sent` 和人工 receipt 均满足 06 deadline。
- [ ] run ID 串联 release/object/provider/evidence/Incident/Plan/Approval/Command/Report/Delivery，缺项即失败。

## A03 执行 Restart Rollout 与安全负向恢复 Gates

**What to build:** 第一轮完成后，Operator 可以证明 stateful process crash/rollout、Connector/Loki unavailable、unauthorized Approval 和 stale Change 都安全降级并恢复，不靠 DB patch 或自动 mutation retry。

**Blocked by:** A02 完成第一轮真实 Alert-to-Report.

- [ ] 顺序删除每个 stateful Pod并验证 PVC/governance/config/history保留；逐个 rollout Deployment/Alloy 后 reapply 收敛。
- [ ] Connector scale-to-zero 阻止 evidence/dry-run/grant/dispatch，恢复后 heartbeat/read verification ready。
- [ ] Loki unavailable 返回 bounded error且不能形成 verified log Evidence，恢复后 retained/new logs可查。
- [ ] 无 Authority User 不能读取 diff、Approval 或产生 grant。
- [ ] Operator 制造 metadata resourceVersion drift 后 Connector 返回 stale、零 approved mutation、无自动 retry。
- [ ] 每项保留 before/during/after status、event/journal ref 和失败诊断。

## A04 完成第二轮 Rerun Cleanup 与 Promotion Evidence

**What to build:** 恢复测试后，新 run reopen 同一 Incident 并完整产生独立 Investigation/Change/Approval/Report v2/Notification；fixture cleanup 后治理历史仍可公开读取，release 得出 promote/no-promote 结论。

**Blocked by:** A03 执行 Restart Rollout 与安全负向恢复 Gates.

- [ ] 新 Job controller UID 在 24h window 内 reopen 原 Incident，不复用旧 Evidence/Approval/grant。
- [ ] 第二轮重复真实 diagnosis、approved recovery、stabilization 和实际 Notification receipt。
- [ ] Report v2 immutable，v1 hash/content 保持不变。
- [ ] 删除 run/base 后 fixture namespace 消失，Resource Target unavailable，产品与两轮治理历史保留。
- [ ] final manifest 覆盖 08 全部 mandatory gate、artifact SHA256 和三类角色 attestation；缺项或 secret exposure 阻止 promotion。
