# Tickets: AIOps Console Operational Navigation

把已有和 Pilot 已确定的领域能力暴露为可工作的 Console 入口，解决 Console 只有“事件”和“平台状态”而显得没有功能的问题。Platform Status 交互决策见 `../aiops-pilot-ready/issues/07-prototype-setup-and-platform-status.md`。

最终主导航只有 `事件`、`变更`、`资源`、`报告`、`平台状态`；`平台管理` 继续从 User menu 进入。通知配置、审计、搜索、Dashboard、知识库和单独 Approval inbox 不增加顶层入口。

Work the **frontier**: any ticket whose blockers are all done. 每张票完成时对应入口必须可用；不得先放一个指向空页面的导航项。

## N01 建立共享 Console Shell 与真实导航

**What to build:** 已登录 User 在所有 Console 页面看到一致、可访问、能反映当前 route 的桌面与窄屏导航；现有 Incident、Report 和 `/admin` 页面不再各自承担或复制 shell。

**Blocked by:** None — can start immediately.

- [ ] Production shell 统一拥有 brand、主导航、User menu、role visibility、退出和窄屏菜单，领域页面只渲染自己的内容。
- [ ] 首次交付只显示已有可用 route；后续票完成时再加入对应入口，不出现 disabled、coming soon 或空 Dashboard。
- [ ] 当前 route、键盘焦点、可访问名称和 390px 无横向溢出有定向测试。
- [ ] `/admin` 仅 Platform Administrator 可见且继续从 User menu 进入；普通 User 不因导航隐藏替代 API authorization。
- [ ] 不增加导航 registry framework、第二套 UI library、客户端权限缓存或 browser-side workflow state machine。

## N02 交付有权限范围的资源工作区

**What to build:** User 可以从 `资源` 查看自己有权访问的 Cluster、Service 和 Deployment Target，快速识别 offline、unbound 和 unavailable 资源；Platform Administrator 可以从同一事实进入 `/admin` 做详细治理。

**Blocked by:** N01 建立共享 Console Shell 与真实导航.

- [ ] Gateway 通过 `/api/v1` 提供 actor-scoped Resource Catalog read projection，不暴露 admin-only notes、credential 或其他 Team 的资源。
- [ ] `/resources` 按 Cluster、Environment、Team/Service 和 binding/runtime state 展示真实资源，不复制 Connector 或 Catalog 状态。
- [ ] URL 拥有筛选与选中资源，刷新和返回可恢复；没有结果、Connector offline、unbound 与已删除 target 有不同状态。
- [ ] 普通 User 只有只读能力；Platform Administrator 的治理动作 deep-link 到 `/admin` 对应领域，不在资源页复制编辑表单。
- [ ] OpenAPI producer/consumer、scope denial、桌面与窄屏视图通过定向测试后，`资源` 才进入主导航。

## N03 交付跨 Incident 的变更中心

**What to build:** 有权 User 可以从 `变更` 找到需要自己处理的 Change Request、Approval 和 Execution outcome，审阅 exact diff 并回到来源 Incident，而不用逐个打开 Incident 寻找待办。

**Blocked by:** N01 建立共享 Console Shell 与真实导航; Pilot Generic Kubernetes Change implementation based on `../aiops-pilot-ready/issues/09-define-generic-kubernetes-change-contract.md`.

- [ ] Change Request owner 提供 actor-scoped list/detail projection，覆盖 `needs_input`、`awaiting_approval`、`executing`、`paused/unknown` 和 terminal outcome。
- [ ] `/changes` 默认突出当前 User 可输入、可审批或必须 reconciliation 的项目，并允许按状态与 Environment 筛选。
- [ ] `/changes/:changeId` 展示来源 Incident、Evidence refs、frozen target、API Server dry-run exact diff、risk、Approval、Execution 和 rollback/outcome history。
- [ ] approve、cancel、accept reconciliation 等动作调用 Change owner 的既有 public command，不在 Console 建第二套状态机，也不把模型输出当授权。
- [ ] 无 Authority 的 User 看不到受限 diff 且 API fail closed；pending 数量只能来自同一 actor-scoped response。
- [ ] OpenAPI contract、authorization、stale/Unknown Outcome 和窄屏 exact diff 可读性通过后，`变更` 才进入主导航。

## N04 交付 Incident Report 资料库

**What to build:** User 可以从 `报告` 找到有权访问的 draft 和已发布 Incident Report，按 Incident/Service/time 定位并打开现有 Report workspace，而不是记住 Incident URL。

**Blocked by:** N01 建立共享 Console Shell 与真实导航.

- [ ] Incident Report owner 提供 actor-scoped summary list，包含 Incident identity、Service、version、draft/published state 和 relevant time，不返回完整 narrative 列表 payload。
- [ ] `/reports` 区分待完成 draft、最新 publication 和 reopened Incident 的多版本历史；筛选保存在 URL。
- [ ] 选择报告进入现有 Incident-scoped Report route，编辑、发布和 immutable version 规则不复制到列表页。
- [ ] 没有 Report 的 active Incident 不伪造 draft；无 scope 的 Report 不泄露存在性。
- [ ] OpenAPI contract、scope、version ordering、empty/loading 和 390px 视图通过后，`报告` 才进入主导航。

## N05 发布真实 Platform Status 并移除评审开关

**What to build:** User 可以从 `平台状态` 持续查看 Model、Notification、Connector/Cluster 和 Observability 的真实 readiness；Platform Administrator 可以恢复失败 capability，普通 User 只读，且页面不再表现为设计原型。

**Blocked by:** N01 建立共享 Console Shell 与真实导航; Pilot readiness owner implementations based on issues 03, 04, and 05 under `../aiops-pilot-ready/issues/`.

- [ ] `/api/v1/platform/status` 聚合 owner response，不复制 integration state，不返回全局 `setup_complete/all_ready`。
- [ ] `/platform` 使用 07 选定的 capability rail，覆盖 `ready`、`not_ready`、`skipped`、owner unavailable、configuration revision 和安全 reason code。
- [ ] Incident workspace 在任意 readiness 下可进入；`skipped` 明确不计 ready，required capability 不可 skip。
- [ ] Platform Administrator 的 configure/test/retry 进入或调用真实 owner workflow；普通 User 无 mutation 控件且看不到 endpoint、recipient 或 secret metadata。
- [ ] `/admin` 继续拥有详细配置，Platform Status 只拥有跨 capability overview 与 recovery entry，不复制管理表单。
- [ ] 删除 variant/scenario 评审控件和 in-memory fake state；真实 contract、权限、桌面/390px 和 owner partial failure 通过后，`平台状态` 才进入主导航。
