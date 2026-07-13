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

- [ ] Diagnosis 独占 encrypted credential、endpoint/model/timeout、revision 和 verification record；Gateway 不复制配置。
- [ ] external/cluster-internal endpoint 分别执行 SSRF、DNS rebinding、redirect 和 TLS policy。
- [ ] test API durable 返回 operation identity；tool call、structured JSON 与 nonce 任一不匹配均 `invalid_response`。
- [ ] credential/config change 使旧 verification stale；确定性 rejection failed，瞬时故障只降 availability。
- [ ] Diagnosis 冻结 provider revision，失败明确结束且不回退 keyword diagnosis、不自动重跑。
- [ ] 正常产品配置不再读取 `AIOPS_MODEL_*` environment fallback；该路径只允许定向测试使用。
- [ ] Admin UI、public safe status、encryption、revision concurrency 和 provider error taxonomy 有定向测试。

## S03 把 Notification Test Delivery 绑定到 Revision Readiness

**What to build:** Platform Administrator 可以验证 exact Notification Destination revision 并显式选为 Pilot Route；只有真实 durable test Delivery `sent` 才 ready，修复 credential 后 pending work 安全恢复。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态; C01 建立共享 Console Shell 与真实导航.

- [ ] Destination revision change 使 verification stale 并暂停 pending Delivery，不消耗 attempt。
- [ ] test 复用真实 Notification Delivery/Apprise path、bounded retry 和 operation identity，不建第二套测试状态机。
- [ ] terminal `sent` 才 verified；dead-letter/credential rejection 使用安全 reason code且不假成功。
- [ ] verified Destination 仍需管理员显式选择 Pilot catch-all Route；测试本身不改 routing。
- [ ] Admin UI、public safe status、masked audit、revision concurrency 和 restart recovery 有定向测试。

## O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics

**What to build:** Canonical bundle 使用真实 Prometheus、Alertmanager 和 kube-state-metrics 收集 AIOps/verification metrics、求值 rule 并以 authenticated `send_resolved` webhook 驱动 Gateway Incident。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay.

- [ ] 使用 native manifests、immutable digest、独立 Prometheus PVC 和已确定 resource/7d retention；不依赖 Operator CRD。
- [ ] static/file discovery 只抓取声明目标，external `cluster` label 与 Connector Cluster identity 一致。
- [ ] Alertmanager route 只把明确 `aiops_route` alert 发到 Gateway，使用 bootstrap token 且不暴露外网。
- [ ] target、rule、firing/resolved webhook 和 MCP guarded query 都有真实集成检查。
- [ ] 删除 Python compatibility metrics、constant series、label-only alert 和 manual webhook acceptance path。

## O02 部署真实 Loki 与 Alloy 日志链路

**What to build:** Canonical bundle 使用 single-binary Loki 和 per-node Alloy 从 Kubernetes Pod log API 收集真实 stdout/stderr，Diagnosis 只能经 MCP Loki guarded query 获得 evidence。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay.

- [ ] Loki 使用 TSDB v13、filesystem、10Gi PVC、compactor 和 168h retention，resource limit 采用已验证基线。
- [ ] Alloy DaemonSet 按 node discovery/relabel，保留 cluster/namespace/pod/container labels，不使用 hostPath、hostPort 或 aggregator。
- [ ] RBAC 只覆盖 Pod discovery/log read，不读 Secret 或 Operator CRD。
- [ ] Loki readiness、真实 unique log query、MCP evidence ref 和 owner unavailable error 有集成检查。
- [ ] 删除 synthetic push、内存 log backend 和 collector-side business log filtering acceptance path。

## C02 交付有权限范围的资源工作区

**What to build:** User 可以从 `资源` 查看自己有权访问的 Cluster、Service 和 Deployment Target，识别 offline、unbound、unavailable；管理员从同一事实 deep-link 到 `/admin` 治理。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; S01 完成多 Connector Enrollment 与 Read Verification.

- [ ] `/api/v1` 提供 actor-scoped Resource Catalog summary，不暴露其他 Team 或 admin-only detail。
- [ ] `/resources` 展示真实 ownership/binding/runtime state，筛选和选中资源由 URL 拥有。
- [ ] 普通 User 只读；管理员编辑继续由现有 owner/admin workflow 完成，不复制表单。
- [ ] scope denial、offline/unbound/deleted state、OpenAPI producer/consumer 和窄屏有定向测试。
- [ ] 页面可用后才把 `资源` 加入共享导航。

## C03 交付 Incident Report 资料库

**What to build:** User 可以从 `报告` 找到有权访问的 draft 与 immutable publications，并打开现有 Incident-scoped Report workspace。

**Blocked by:** C01 建立共享 Console Shell 与真实导航.

- [ ] Report owner 提供 actor-scoped summary list，不在列表返回完整 narrative/history payload。
- [ ] `/reports` 区分 draft、latest publication 和 reopened Incident 多版本；筛选由 URL 拥有。
- [ ] 编辑和 publish 只发生在现有 Report route，不复制 Report state machine。
- [ ] scope non-disclosure、version ordering、empty/loading、OpenAPI 和 390px 有定向测试。
- [ ] 页面可用后才把 `报告` 加入共享导航。

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

- [ ] Authority 与 Environment 相交，并在 proposal、diff read、Approval、grant 和 dispatch 时重复校验。
- [ ] Approval 冻结 ordered Changes、dry-run diff/hash、risk、post-check、rollback policy 和 phase expiry。
- [ ] fresh auth、reason 和 exact target confirmation 满足后才进入 approved；同一授权 User 可 self-approve。
- [ ] dry-run 10m、approved start 15m 到期显式 expired，不自动 refresh/reapprove。
- [ ] 无 Authority 的 diff non-disclosure、stale revision、idempotency 和 immutable audit 有 contract/UI 测试。

## K04 执行单个 Generic Kubernetes Change

**What to build:** Connector 使用 60s single-use Execution Grant 执行一个 approved create/patch/delete，重验全部 precondition/hash，并以 frozen structured post-check 得出可信 outcome。

**Blocked by:** K03 审批 Exact Change Plan Phase; P02 交付单入口 Canonical Kustomize Overlay.

- [ ] Release 只给 Connector 专用 wildcard read/create/patch/delete ClusterRole，明确排除 update/deletecollection/impersonate/bind/escalate。
- [ ] Connector claim 前校验 grant、change hash、UID/resourceVersion/old-value test；drift 返回 stale 且零 mutation。
- [ ] started execution 默认 5m、最大 30m；later grant 只在 prior trustworthy terminal/post-check 后签发。
- [ ] Connector journal 先 durable started/result 后 handoff；重复 command/grant 不重复 mutation。
- [ ] Gateway/Connector/Console 端到端覆盖 success、API rejection、stale、post-check failure 和 Kubernetes audit identity。

## K05 顺序执行 Multi-object Plan 并按 Frozen Inverse 回滚

**What to build:** 一个 approved Phase 可以顺序执行多个 Kubernetes Changes，首个失败即停止；已完成 step 只按 Approval 冻结的 inverse change 逆序 rollback，不声称跨对象原子性。

**Blocked by:** K04 执行单个 Generic Kubernetes Change.

- [ ] 每 step 独立 grant/journal/result/post-check，后续 step 不会提前领取。
- [ ] failed/stale/unknown step 停止 Phase；rollback 只在 frozen condition 成立时执行 exact inverse。
- [ ] API surface-changing dependency 拆成新 Phase，重新 discovery/dry-run 并单独审批。
- [ ] start 前 cancel 撤销 grant；start 后 `cancel_requested` 只停止后续 grant并等待当前 outcome。
- [ ] `stop_only` 与 `rollback_completed` policy、rollback failure 和 audit timeline 有端到端测试。

## K06 通过 Secure Input 执行 Sensitive 或 Irreversible Change

**What to build:** User 可以在不把 plaintext 交给模型、API response、diff 或 audit 的情况下提供 sensitive value；Cluster Change Authority 可以明确审批无法可靠 rollback 的 change。

**Blocked by:** P01 建立幂等 Bootstrap 安装状态; K04 执行单个 Generic Kubernetes Change.

- [ ] Secure Input 独立于 Change Request，模型只见 opaque placeholder；Gateway 使用 CSPRNG 或 User input。
- [ ] Gateway/Connector 以独立 change encryption key 保存 ciphertext/nonce/hash，plaintext 只在内存中短暂存在。
- [ ] Approval/diff/report 只显示 key name/hash；terminal rollback window 后删除 ciphertext。
- [ ] key loss 使未完成 plan `secure_input_unavailable`，不重新生成 credential或执行 placeholder。
- [ ] Irreversible Change 显示 concrete loss、`rollback: unavailable`，要求 fresh auth/reason/重新输入 exact target。
- [ ] Secret redaction、key rotation/loss、cluster authority 和 irreversible confirmation 有安全测试。

## K07 保守 Reconcile Unknown Outcome

**What to build:** response loss 或 Connector restart 后，系统区分 confirmed result、Observed Effect 和 Unknown Outcome，暂停后续执行且绝不自动 retry。

**Blocked by:** K05 顺序执行 Multi-object Plan 并按 Frozen Inverse 回滚; K06 通过 Secure Input 执行 Sensitive 或 Irreversible Change.

- [ ] 只有可信 Connector journal terminal result 可 confirmed succeeded/failed。
- [ ] exact live state + post-check 但无 attribution 记录 Observed Effect；不匹配/模糊保持 Unknown Outcome。
- [ ] 两者暂停 Phase，User 接受 reconciliation evidence 后模型才能基于 live state重新规划。
- [ ] governance history 永久保留 redacted plan/diff/Approval/grant/outcome；Connector terminal journal 30d bounded cleanup。
- [ ] restart recovery、late result、no retry、effect attribution 和 audit projection 有定向测试。

## K08 迁移 Caller 并退役有限 Mutation Contract

**What to build:** Workbench、Report、Notification 和 verification 全部使用 Generic Change Request/Plan；完成迁移后删除旧 direct Recommended Action Approval 与 typed restart/scale/rollback product contract。

**Blocked by:** K07 保守 Reconcile Unknown Outcome.

- [ ] Workbench 从 Recommendation 创建 Change Request，不再直接批准并执行 action。
- [ ] Report/Notification 投影新的 Plan/Phase/Approval/Execution/rollback/reconciliation history。
- [ ] verification restart 使用 canonical RFC 6902 annotation patch 与 generic Connector execution。
- [ ] 同一变更删除旧 public routes、OpenAPI schema、Gateway owner、Connector typed mutation branch 和对应 UI caller。
- [ ] 不保留 wrapper、双写、双读或无退出条件 compatibility path。
- [ ] replacement contract、legacy absence、architecture boundary 和直接消费者测试全部通过。

## C04 交付跨 Incident 的变更中心

**What to build:** 有权 User 可以从 `变更` 找到需要输入、审批、reconciliation 或查看 outcome 的 Change Request，并审阅 exact diff、执行历史和来源 Incident。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; K08 迁移 Caller 并退役有限 Mutation Contract.

- [ ] Change owner 提供 actor-scoped list/detail projection，覆盖 active、paused 和 terminal phase/outcome。
- [ ] `/changes` 默认突出当前 User 可处理项；status/Environment filter 由 URL 拥有。
- [ ] detail 展示 Evidence refs、target、dry-run diff、risk、Approval、Execution、rollback/reconciliation history。
- [ ] approve/cancel/accept reconciliation 调 owner command，不在 Console 建状态机。
- [ ] Authority non-disclosure、pending count、OpenAPI、stale/unknown 和窄屏 diff 有定向测试。
- [ ] 页面可用后才把 `变更` 加入共享导航。

## S04 发布 Optional Web Setup 与真实 Platform Status

**What to build:** 所有 User 可以持续查看四项真实 capability status；Platform Administrator 可以配置、验证、skip optional capability并恢复失败，Incident workspace 始终可进入。

**Blocked by:** C01 建立共享 Console Shell 与真实导航; S01 完成多 Connector Enrollment 与 Read Verification; S02 配置并验证真实 Model Provider; S03 把 Notification Test Delivery 绑定到 Revision Readiness; O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics; O02 部署真实 Loki 与 Alloy 日志链路.

- [ ] Gateway 聚合 owner response，不复制 configuration、不持久化全局 `setup_complete/all_ready`。
- [ ] capability 分别投影 readiness/configuration/revision/verification/availability/safe reason，owner timeout 不拖垮其他项。
- [ ] skip 记录 actor/reason/time且永不算 ready；保存新配置自动恢复 active。
- [ ] `/platform` 使用 07 选定 capability rail；admin 有 configure/test/retry，普通 User 只读安全摘要。
- [ ] `/admin` 保留详细领域配置；删除 prototype variant/scenario 和 in-memory fake state。
- [ ] desktop/390px、re-entry、partial owner failure、fresh-auth 和 secret non-disclosure 有端到端测试。

## V01 交付 Version-matched Controlled Verification Fixture

**What to build:** Operator 可以显式安装一个隔离 verification workload，并以 Kubernetes 生成 run ID 触发不会被 liveness 自动修复的真实 readiness fault；cleanup 不触碰产品治理历史。

**Blocked by:** P02 交付单入口 Canonical Kustomize Overlay; O01 部署真实 Prometheus Alertmanager 与 kube-state-metrics; O02 部署真实 Loki 与 Alloy 日志链路; K08 迁移 Caller 并退役有限 Mutation Contract.

- [ ] optional `base` 创建固定 namespace、quota/limit、restricted security、NetworkPolicy、ServiceAccount、Deployment/Service。
- [ ] app 提供 live/ready/metrics/internal trigger；fault 使用 Job controller UID 幂等 latch，真实 stdout log 与 metric 带 bounded run ID。
- [ ] `run` 只创建 trigger Job；重复 Pod request 幂等，不同 run 在 active fault 时 conflict。
- [ ] Prometheus rule exact 匹配 fixture 并携带 correlation labels；恢复使用 approved pod-template annotation rollout。
- [ ] immutable image、resource ceiling、RBAC/network isolation、rerun 和 delete overlays 有定向集成测试。

## P03 打包最终 Immutable Pilot Release

**What to build:** Release Operator 可以发布并验证一个 self-contained `aiops-pilot-vX.Y.Z.tar.gz` 与 `SHA256SUMS`，新 Operator 解压后无需仓库或构建工具即可安装完整 candidate。

**Blocked by:** C02 交付有权限范围的资源工作区; C03 交付 Incident Report 资料库; C04 交付跨 Incident 的变更中心; S04 发布 Optional Web Setup 与真实 Platform Status; V01 交付 Version-matched Controlled Verification Fixture.

- [ ] archive 顶层就是唯一 Kustomize overlay，包含本地 manifest、verification overlays 和简短安装/恢复说明。
- [ ] 所有 workload image 为完整公开可拉取 digest；无 remote base、zero digest、mutable tag 或 source checkout dependency。
- [ ] Console/Gateway OpenAPI producer/consumer、owner image/config revision 和 release version一致。
- [ ] package test 从空目录校验 checksum、render、image inventory 和禁止路径。
- [ ] 当前只承诺 clean install/same-version reapply，不暗示 upgrade/downgrade/backup/uninstall。

## A01 自动执行 Package Install 与 Setup Gates

**What to build:** Release verifier 可以为一个 clean non-production Cluster 创建脱敏 acceptance evidence bundle，自动执行 package、install、HTTP access、first-login security 和 setup readiness gates，并由人员完成必要 attestation。

**Blocked by:** P03 打包最终 Immutable Pilot Release.

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
