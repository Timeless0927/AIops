# Tickets: AI 对话完整能力

这些票据按依赖顺序实现完整的 AI 对话、会话管理、消息分支、附件、assistant-ui、Investigation Handoff 和中文渐进披露界面。来源规格：[PRD.md](./PRD.md)。

Work the **frontier**: any ticket whose blockers are all done. 当前从 T01 开始；一个票据完成并验证后，再开启所有 blocker 已完成的下一张票。

## T01 接入 Console 壳与中文导航

**What to build:** User 登录后进入使用已选 shadcn blocks 组合的真实 Console 壳；导航、身份信息和页面入口来自现有产品数据，“Chat”统一显示为“AI 对话”，不再出现示例品牌、假用户、假项目或无效链接。

**Blocked by:** None — can start immediately.

- [x] `login-03` 接入现有登录 contract，保留 Cookie Session、CSRF、错误状态和登录后跳转。
- [x] `sidebar-07` 接入现有路由和权限可见性，导航包含“AI 对话”、事件、报告和有权限时才出现的管理入口。
- [x] 示例 Acme、Projects、Teams、假用户、`#` 链接和未实现动作全部删除。
- [x] `signup-01` 不接入没有后端支持的自助注册流程，也不出现在生产导航。
- [x] 用户可见按钮、菜单、状态、错误和空状态使用中文；Kubernetes、MCP、API、SSE 等必要术语保留英文。
- [x] 桌面和移动视口无页面级横向溢出，键盘可操作，可见焦点和可访问名称保留。
- [x] Console 定向组件测试、TypeScript no-emit 和 production build 通过。

## T02 完成 AI 对话会话管理

**What to build:** User 可以在真实 AI 对话列表中创建、切换、重命名、置顶、归档、恢复、永久删除和搜索自己的 Chat Session；状态由 Gateway 持久化并通过 OpenAPI 和 SSE 投影到 Console。

**Blocked by:** T01 接入 Console 壳与中文导航.

- [x] 修改超过 500 行的既有 Chat Session owner 前，任务记录明确所属 Module、公开 Interface 和定向测试 selector；新会话能力不得继续堆入无边界 Store。
- [x] 现有 Chat Session 数据通过 forward migration 原地升级，不复制旧实现、不双写、不丢失消息或 Handoff 引用。
- [x] 会话支持安全标题、手动重命名、置顶/取消置顶、归档/恢复和永久删除；手动标题不再被自动标题覆盖。
- [x] 正常和归档会话不再因 30 天到期自动删除；永久删除同时清理没有其他保留引用的消息数据。
- [x] 置顶会话优先按最近活动排序；归档自动取消置顶并从默认列表隐藏。
- [x] 搜索覆盖当前 User 自己的标题、消息正文、附件名称和可用的已提取文本，并支持正常、置顶和归档筛选。
- [x] 所有写操作验证 ownership、CSRF、idempotency 和请求冲突；任何响应不泄露其他 User 的会话是否存在。
- [x] OpenAPI、generated Console types、Gateway HTTP Adapter、SSE 事件和直接 contract 测试同步更新。
- [x] Console 会话列表提供搜索、筛选、重命名、置顶、归档/恢复和带 AlertDialog 的永久删除，错误与空状态使用中文。
- [x] Gateway Module、HTTP contract 和 Console 定向测试通过。

门禁记录（T02）：Chat Session Module 为 `apps/aiops_k8s_gateway/chat_sessions.py`，公开 Interface 是创建、列表/搜索/筛选、读取、发送、重试、更新和永久删除；HTTP Adapter 为 `chat_http.py`，定向 selector 为 `tests/test_gateway_chat.py` 与 `tests/test_gateway_v1_chat_contract.py`。本票触碰的既有 500+ 行 owner 保持单一职责，未新增第二套 Store；管理字段通过 migration 51 原地升级。

## T03 增加不可覆盖的消息分支

**What to build:** User 编辑旧问题或重新生成 AI 回答时会得到一个新分支，可以在分支间切换和比较，同时保留原消息、附件、回答和 Handoff 引用。

**Blocked by:** T02 完成 AI 对话会话管理.

- [x] Chat Message storage 从单一路径迁移为不可覆盖的父子分支关系，并保留现有消息 ID 和顺序语义。
- [x] 编辑 User 消息创建新的 User/Assistant 分支，重新生成创建新的 Assistant sibling，不更新或删除原消息。
- [x] 每个分支只向模型提供该分支祖先链，不能混入其他分支内容。
- [x] 运行中的回复不能编辑或切换；失败、重复和并发操作返回稳定错误且不留下半个分支。
- [x] retry 继续只重试原失败消息；reload/重新生成使用新分支，两种语义不得混淆。
- [x] 当前分支、分支数量和可切换关系由 Gateway contract 和 SSE cursor 持久投影。
- [x] Handoff 仍使用真实消息 ID，服务端只接受当前可见分支中明确选定且已完成的消息。
- [x] assistant-ui BranchPicker 所需 contract 由 Console generated types 表达，不创建第二套客户端分支状态机。
- [x] Module、HTTP contract 和 Console 分支交互定向测试覆盖正常、失败、并发和断线恢复路径。

门禁记录（T03）：Chat Session Module 的分支能力由 `apps/aiops_k8s_gateway/chat_branches.py` 拥有，`chat_sessions.py` 只保留显式 `edit`、`reload`、`switch_branch` 公开 Interface；HTTP Adapter 为 `chat_http.py`。定向 selector 为 `tests/test_gateway_chat.py`、`tests/test_gateway_chat_handoff.py`、`tests/test_gateway_v1_chat_contract.py`、`src/chat/chat-page.test.tsx` 与 `e2e/console-chat-branches.spec.ts`。本票结束时 `chat_sessions.py` 为 766 行，分支 owner 为 378 行，均未超过 800 行门禁。

## T04 建立 Chat Attachment 存储与安全链路

**What to build:** User 可以上传受支持的文件和图片，看到上传、扫描、解析和失败状态，并只通过 Gateway 访问自己的附件；未通过验证的内容永远不会进入模型。

**Blocked by:** T02 完成 AI 对话会话管理.

- [x] Chat Attachment 由独立内聚 Module 管理 metadata、hash、状态、引用和物理文件生命周期，Gateway HTTP Adapter 不承载文件领域决策。
- [x] 允许 PNG、JPEG、WebP、PDF、TXT、LOG、Markdown、JSON、YAML 和 CSV；拒绝 Office、压缩包、可执行文件、证书、kubeconfig 和凭据文件。
- [x] 每条消息最多 5 个附件、单文件最多 20MB、总计最多 50MB；Gateway 不信任浏览器声明的大小、扩展名或 MIME。
- [x] 文件名经过规范化且不参与路径拼接；真实文件签名、MIME、大小、内容 hash 和解析上限均在 Gateway 信任边界验证。
- [x] 上传状态覆盖 pending、uploading、scanning、ready、rejected 和 failed；只有 ready 附件可以随消息发送。
- [x] 恶意文件扫描 fail closed：扫描器不可用、超时或报错时拒绝附件，不降级为未扫描上传。
- [x] 文本敏感信息检查复用现有安全规则；疑似 Token、密码、Credential 或 Secure Input 不进入模型、日志、审计或搜索索引。
- [x] 二进制文件保存在 Gateway 现有持久化卷，元数据在 `gateway.db`；不引入 Assistant Cloud、MinIO、浏览器直传或第二个 persistence owner。
- [x] 上传、查询、鉴权下载、发送前删除和失败重试 contract 具备 ownership、CSRF、idempotency 和不泄露存在性的错误行为。
- [x] 内容 hash 可以复用同一物理文件，但每个 Chat Session 保留独立授权引用；只有零保留引用时才删除物理文件。
- [x] 安全定向测试覆盖 MIME spoofing、路径穿越、大小/数量超限、扫描失败、解析炸弹、敏感内容、跨 User 访问、重复上传和清理。

门禁记录（T04）：Chat Attachment Module 为 `apps/aiops_k8s_gateway/chat_attachments.py`（任务开始时 532 行，单一职责），公开 Interface 是 `reserve`、`upload`、`list`、`get`、`download`、`delete`、`retry`、`bind`/`bind_in`、`matching_session_ids` 与 `collect_garbage`；定向 selector 为 `tests/test_gateway_chat_attachments.py`、`tests/test_gateway_v1_chat_attachment_contract.py` 与 `apps/aiops_console_web/src/chat/chat-page.test.tsx`。本票修改保持该 Module 单一 owner，不新增第二套持久化或 HTTP 领域决策。

## T05 让附件受控进入模型

**What to build:** User 发送 ready 附件后，AI 可以读取受支持的 PDF、文本和图片；附件内容被明确视为不可信 User context，不能扩大工具、资源或执行权限。

**Blocked by:** T04 建立 Chat Attachment 存储与安全链路.

- [x] PDF 和文本附件产生有界、可追溯的提取内容，解析失败或超限明确拒绝，不静默截断成误导性输入。
- [x] Model Provider verification 明确当前 revision 是否支持图片输入；不支持时在发送前拒绝图片并返回中文可操作原因。
- [x] 模型请求只携带当前分支 ready 附件的受限内容和稳定 attachment identity，不暴露 Gateway 文件路径或下载凭据。
- [x] 附件内容使用不可信上下文边界，不能声明 capability、改变 frozen resource scope、创建 Evidence、Approval、Execution Grant 或 Connector Command。
- [x] 知识模式和环境模式均保留现有只读工具策略、scope refreeze、skill revision 和 completion validation。
- [x] attachment identity、解析结果和模型使用状态可通过 Chat Message contract 投影，但原始二进制和敏感提取内容不进入 SSE、日志或审计。
- [x] Diagnosis 定向测试覆盖文本、PDF、支持/不支持图片模型、恶意提示、工具扩权尝试、失败重试和 checkpoint 幂等恢复。
- [x] Gateway 到 Diagnosis 的直接 contract 测试证明附件不会改变现有授权与治理边界。

门禁记录（T05）：附件模型输入仍由 Chat Attachment Module `apps/aiops_k8s_gateway/chat_attachments.py` 拥有，公开 Interface 增加 `model_inputs`、`contains_image`、`branch_contains_image` 与 `message_views`/`message_views_in`；Chat Session Module 只装配当前分支消息与附件，`chat_sessions.py` 从 766 行增至 800 行，未跨越超大文件门禁。Diagnosis 的公开 Interface 仍为 `answer_governed_chat`，Model Provider capability 归属既有 `ModelProviderConfiguration` 与 immutable `ProviderRevision`。定向 selector 为 `tests/test_gateway_chat_attachments.py`、`tests/test_gateway_chat.py`、`tests/test_gateway_v1_chat_attachment_contract.py`、`tests/test_diagnosis_governed_chat.py`、`tests/test_diagnosis_knowledge_chat.py`、`tests/test_diagnosis_provider.py`、`tests/test_model_provider_configuration.py`、`tests/test_gateway_v1_model_provider_contract.py` 与 Console `npm run build`。

## T06 接入 assistant-ui 基础运行时

**What to build:** User 使用 assistant-ui 的成熟 Thread、Message、Composer 和 Thread List 交互浏览和发送 AI 对话，同时 Gateway 与 TanStack Query 继续作为唯一服务端状态权威。

**Blocked by:** T01 接入 Console 壳与中文导航; T02 完成 AI 对话会话管理.

实施门禁（T06）：`apps/aiops_console_web/src/chat/chat-page.tsx` 在本票开始时为 646 行，所属 Console Chat Module，公开 Interface 为路由组件 `ChatPage` 与展示组件 `ChatView`；本票将 assistant-ui 的纯 Gateway message 映射迁入 `chat-runtime.ts`，页面保留 Query/SSE 与 Gateway mutation 装配，定向 selector 为 `src/chat/chat-runtime.test.ts`、`src/chat/chat-page.test.tsx` 与 `e2e/console-chat-*.spec.ts`。

- [x] 安装并锁定当前兼容 React 19 的 assistant-ui 核心包，只引入本票真实使用的包。
- [x] `ExternalStoreRuntime` 将 Gateway Chat Message、status、扩展字段和稳定 ID 映射为 assistant-ui message parts。
- [x] Thread List adapter 调用 Gateway 创建、切换、重命名、置顶、归档、恢复、删除和搜索能力，不使用 Assistant Cloud。
- [x] TanStack Query 继续拥有 server state，页面本地 state 只拥有草稿、展开状态和临时交互；不增加 Zustand/Redux 或第二套会话状态机。
- [x] Composer 发送、失败 retry、scope selection、CSRF、idempotency 和 EventSource invalidate 继续走现有 Gateway client。
- [x] Tool Activity、Evidence reference、uncertainty、next step、completion 和 skill version 信息完整映射，不压缩成只有 role/content。
- [x] Handoff 保留现有真实 message ID、权限、幂等和 Human Input 语义，不交给 assistant-ui 默认 transport。
- [x] 旧的重复 Chat 展示实现被删除，不保留长期 wrapper、双 UI 或模式开关。
- [x] Adapter contract 和 Console 组件定向测试覆盖发送、失败、重试、切换会话、SSE 刷新和 ownership 错误。

验收记录（T06）：复用已锁定的 `@assistant-ui/react@0.15.8`；`chat-runtime.ts` 提供 Gateway message 与 Thread List adapter，`ChatView` 使用 Thread/Message/Composer/ActionBar primitives，Query/SSE/Gateway client 仍是 server-state 与 mutation owner。定向验证为 `npm test -- src/chat/chat-runtime.test.ts src/chat/chat-page.test.tsx`（8 passed）、`tsc --noEmit`、`npm run build`、Playwright `console-chat-management.spec.ts`、`console-chat-branches.spec.ts` 与 `console-chat-runtime.spec.ts`（桌面/390px 移动共 12 passed，发送、失败重试、SSE invalidate、ownership error、截图和页面级横向溢出断言通过）。

## T07 完成 assistant-ui 分支、附件和渐进披露界面

**What to build:** User 在最终 AI 对话界面中使用附件上传、消息分支、搜索和会话操作；核心答案保持清晰，复杂详情按需展开，桌面和移动端都可高效操作。

**Blocked by:** T03 增加不可覆盖的消息分支; T04 建立 Chat Attachment 存储与安全链路; T06 接入 assistant-ui 基础运行时.

门禁记录（T07）：本票仍归属 Console Chat Module；公开 Interface 为 `ChatView`、`ChatPage`、`chatMessageRepository`、`chatThreadListAdapter` 和新增的 Gateway-backed `chatAttachmentAdapter`；定向 selector 为 `src/chat/chat-runtime.test.ts`、`src/chat/chat-page.test.tsx`、`e2e/console-chat-runtime.spec.ts`、`e2e/console-chat-management.spec.ts`、`e2e/console-chat-branches.spec.ts`。`chat-page.tsx` 开始时约 660 行，虽超过 500 行但仍只承载 Chat 页面装配与交互，本票不为行数制造转发层。

- [x] 自定义 AttachmentAdapter 对接 Gateway 上传、进度、扫描、删除、失败重试和发送 lifecycle，不把大文件编码进浏览器状态。
- [x] Composer 支持文件选择、拖拽和图片粘贴，发送前显示名称、缩略图、大小、状态和移除操作。
- [x] BranchPicker、编辑和重新生成调用 Gateway 分支 contract；运行中禁用不安全操作并保持稳定布局。
- [x] 会话列表在桌面可折叠，在移动端使用 Sheet；搜索和归档区不与全局侧栏争抢空间。
- [x] 消息正文、发送状态、失败原因和下一步默认可见；工具活动、Evidence、uncertainty、skill version 和附件解析详情默认折叠。
- [x] 图标按钮使用 Lucide 和 Tooltip，操作集合使用 DropdownMenu，破坏性操作使用 AlertDialog，不制造嵌套卡片。
- [x] loading、empty、offline/reconnecting、uploading、scanning、rejected、failed 和 permission denied 状态全部有中文视图。
- [x] 键盘操作、可见焦点、可访问名称、长文本换行、移动端无溢出和不会遮挡内容通过定向测试。
- [x] Playwright 截图在桌面和移动视口验证非空、无重叠、无横向溢出，并核验附件缩略图和分支控件实际渲染。

验收记录（T07）：`chatAttachmentAdapter` 使用 Gateway 稳定 ID 对接 reserve/upload/retry/delete/send，并以 TanStack Query 与 SSE 恢复 server state；assistant-ui Composer/Message attachment primitives 提供选择、拖拽、粘贴、缩略图与 lifecycle，文件仅保留为 `File`/Gateway URL，不转 data URL。会话栏复用同一 `ChatThreadList`，桌面可折叠、移动端进入 Sheet；治理扩展和附件解析详情默认折叠，常见状态与失败原因已中文化。定向 Vitest 10 passed；TypeScript 与 production build 通过；Chat runtime/management/branches Playwright 在 1440×900 和 390×844 共 10 passed，截图、图片像素、无页面级横向溢出断言通过。

流式修复门禁（T07）：新增 `chat_streaming.py` 作为 Chat response delta、完成和取消的最小 Module；公开 Interface 仍由 `ChatSessions.send` / `retry` / `cancel` 与 Gateway HTTP/SSE contract 暴露，Console 消费 `message.delta` / `message.cancelled`。`chat_sessions.py` 未承载新增实现并收敛至 796 行；定向 selector 为 `tests/test_gateway_chat.py`、`tests/test_gateway_v1_chat_contract.py`、`src/chat/chat-runtime.test.ts`、`src/chat/chat-page.test.tsx`、`src/api/client.test.ts` 与 `e2e/console-chat-*.spec.ts`。

会话修复门禁（T07）：新建会话复用规则归属 `chatThreadListAdapter`，空会话重复点击不创建新记录；Chat Session 的显式删除在事务提交后将附件回收视为最佳努力，不改变已提交结果。定向 selector 增加 `src/chat/chat-runtime.test.ts` 与 `tests/test_gateway_chat.py`；`chat-page.tsx` 仍只负责 Chat 页面装配，未新增分层。

## T08 衔接 Handoff 与事件调查反馈

**What to build:** User 可以把当前分支中选定的消息和附件安全地 Handoff 到已有 Incident 或 User-created Incident，并在成功后选择进入 Workbench 继续反馈，调查材料不受原 AI 对话删除影响。

**Blocked by:** T04 建立 Chat Attachment 存储与安全链路; T07 完成 assistant-ui 分支、附件和渐进披露界面.

大文件门禁（T08）：Gateway Chat Attachment/Handoff Module 的公开 Interface 为 `ChatAttachments.collect_garbage` 与 `ChatHandoffs.execute`，定向 selector 为 `tests/test_gateway_chat_handoff.py`、`tests/test_gateway_chat_attachments.py` 和 `tests/test_gateway_v1_chat_handoff_contract.py`；Console Chat/Workbench Module 的公开 Interface 为 `ChatView`、`ChatPage` 与 `WorkbenchPrototypePage`，定向 selector 为 `src/chat/chat-page.test.tsx`、新增的 Workbench 定向 Vitest 和 T08 Playwright 流程。`chat_attachments.py` 与 `chat-page.tsx` 虽超过 500 行，仍分别保持附件存储/回收与 AI 对话页面装配的单一职责，本票不拆分无关区域。

- [x] Handoff 服务端重新验证当前 User、Chat Session、当前分支、消息状态、附件 ready 状态和目标 Incident/Investigation 权限。
- [x] 选中消息和附件以稳定 hash 和独立保留引用复制为 Human Input material；不成为 Evidence、Approval 或执行授权。
- [x] 删除原 AI 对话只释放 Chat 引用，不删除 Investigation 已保留的附件材料。
- [x] Handoff response 返回目标 Incident 和 Investigation identity，重复 idempotency key 返回同一结果。
- [x] 成功 Dialog 提供“进入事件调查”和“留在 AI 对话”，不强制跳转；目标 Investigation terminal 或不可访问时显示中文原因。
- [x] Workbench 继续使用现有 assertion、correction、retraction、pause、takeover、terminate 和 reinvestigate contract，不建立 Chat/Investigation 双向同步。
- [x] Workbench 的反馈入口、控制动作和详细事件使用 Dialog、Sheet、Accordion 和 AlertDialog 渐进披露，不修改领域状态机。
- [x] Gateway Module、HTTP contract、Console 和 Investigation 直接消费方测试覆盖已有 Incident、新建 Incident、重复请求、terminal Investigation、删除原会话和跨 User 拒绝。

验收记录（T08）：`ChatHandoffs.execute` 重新校验当前分支的已完成消息及其 ready 附件，把消息内容 SHA-256 和附件 SHA-256 写入 Human Input，并以 `chat_handoff_attachments` 独立保留 blob 引用；删除 Chat 后事件、保留引用和物理附件仍存在，幂等重放返回原 Incident/Investigation identity。Console Handoff 成功 Dialog 提供进入调查或留在 AI 对话，terminal/不可访问错误中文化；Workbench 保持原 Human Input 与 control contract，仅用 Dialog、Sheet、Accordion、AlertDialog 优化入口和详情。Gateway/Investigation 定向 pytest 20 passed，Console Vitest 6 passed，TypeScript 与 production build 通过；T08 Playwright 在 1440×900 和 390×844 共 2 passed，截图人工核验非空、无重叠且无页面级横向溢出。

## T09 完成 AI 对话端到端验收

**What to build:** Maintainer 可以用固定测试和真实浏览器流程证明 AI 对话所有首期能力完整可用、中文一致、移动端可操作，并且没有恢复旧 Console 或削弱治理边界。

**Blocked by:** T01 接入 Console 壳与中文导航; T02 完成 AI 对话会话管理; T03 增加不可覆盖的消息分支; T04 建立 Chat Attachment 存储与安全链路; T05 让附件受控进入模型; T06 接入 assistant-ui 基础运行时; T07 完成 assistant-ui 分支、附件和渐进披露界面; T08 衔接 Handoff 与事件调查反馈.

验收门禁（T09）：Console Workbench Module 的公开 Interface 为 `WorkbenchPrototypePage`、`DecisionTrace` 与 `RecommendationsSection`，定向 selector 为 `src/prototype/workbench-page.test.tsx`、`src/prototype/decision-trace.test.tsx`、`src/recommendations/recommendations-section.test.tsx` 和 `e2e/console-chat-handoff.spec.ts`。`workbench-page.tsx` 在本票开始时为 509 行，仍只承载事件调查页面装配与交互；本票仅收敛用户可见文案，不新增业务能力或分层。

- [x] 固定端到端流程覆盖 `login → AI 对话 → 新建 → 重命名 → 置顶 → 搜索 → 归档/恢复 → 上传 → 发送 → 分支 → Handoff → 进入事件调查`。
- [x] 负向流程覆盖跨 User、CSRF、idempotency conflict、不支持类型、超限、扫描不可用、解析失败、不支持图片模型、断线重连和 terminal Investigation。
- [x] OpenAPI snapshot、generated Console types、Gateway producer、Diagnosis consumer 和直接 contract 测试保持一致。
- [x] Console Vitest、TypeScript no-emit 和 production build 通过；受影响 Gateway、Diagnosis 和 Investigation 定向测试通过。
- [x] Playwright 在桌面与移动视口完成截图和交互核验，无页面级横向溢出、不可见焦点、文本遮挡或失效按钮。
- [x] 用户可见范围不存在可汉化但遗漏的英文，特殊英文术语保持一致且不会阻碍理解。
- [x] 不存在 Assistant Cloud、旧 Console、legacy CSS、无效示例链接、第二套 Chat 状态机、Chat/Investigation 双向同步或未扫描附件降级路径。
- [x] 规格中的全部 User Stories 有对应票据和可执行验收证据，未完成项不得标记为完成。

验收记录（T09）：固定流程收敛到 `e2e/console-chat-handoff.spec.ts`，在桌面 1440×900 和移动 390×844 各通过 1 次（2 passed），实际完成登录、导航、新建、重命名、置顶、搜索、归档/恢复、附件上传、发送、重新生成后的分支切换、重复 Handoff 幂等重放和进入事件调查；断言保留焦点、中文 Workbench headings、表单字段和页面级 `scrollWidth`，截图人工核验非空、无遮挡、无失效主操作。

负向矩阵由 `tests/test_gateway_chat.py`、`tests/test_gateway_chat_attachments.py`、`tests/test_gateway_chat_handoff.py`、`tests/test_gateway_v1_chat_contract.py`、`tests/test_gateway_v1_chat_attachment_contract.py`、`tests/test_gateway_v1_chat_handoff_contract.py`、`tests/test_diagnosis_governed_chat.py` 与 Console `src/chat/chat-page.test.tsx`/`src/chat/chat-runtime.test.ts` 覆盖：跨 User、CSRF、idempotency conflict、不支持类型、单文件/总量超限、scanner unavailable、解析超限、图片模型不支持、SSE reconnecting 和 terminal Investigation 均有稳定错误或保守状态。上述 Gateway/Diagnosis/Investigation 定向 pytest 共 97 passed；Console 定向 Vitest 共 29 passed；`tsc --noEmit` 和 `npm run build` 通过（仅既存大 chunk warning）。

OpenAPI 生成命令 `npm run generate:api` 后 `src/api/schema.d.ts` 无差异；Gateway Chat producer、Diagnosis `answer_governed_chat` consumer、直接 HTTP contract 和不可信附件边界测试保持一致。源码未导入 Assistant Cloud、旧 Console 或 legacy CSS，Chat/Investigation 仍是显式单向 Handoff，扫描失败不会降级为可发送附件。

User Story 对应关系：US1、US2-13、US33 由 T01/T02；US14-17 由 T03；US18-22、US25、US34 由 T04/T07；US23-24 由 T05；US26-29 由 T08；US30-32、US35 由 T01/T06/T07/T08/T09。全部 35 条 Story 均有已勾选票据、公开 Interface 定向测试和本票聚合 E2E 证据。
