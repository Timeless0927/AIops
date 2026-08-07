Status: ready-for-agent

# AI 对话完整能力规格

## Problem Statement

Console 当前的 Chat Session 只有创建、发送、失败重试和 Investigation Handoff。用户无法像使用成熟 AI 对话产品一样管理多个会话，也不能重命名、搜索、置顶、归档、删除、创建消息分支或上传文件。当前界面一次展开过多技术信息，且部分英文会阻碍理解。

产品界面需要把 Chat 显示为“AI 对话”，使用 assistant-ui 提供成熟的对话交互，同时保留 AIOps 现有 Gateway、权限、CSRF、幂等、REST、SSE 和 Investigation 领域边界。

## Solution

把“AI 对话”作为 Console 的用户可见名称，完整补齐 Gateway-owned Chat Session 能力：会话管理、搜索、消息分支、文件与图片附件、附件安全、持久化和 Handoff 关联。assistant-ui 只负责对话界面和交互适配，不接管后端事实或另建浏览器端状态机。

界面采用渐进披露：正文优先，工具活动、证据引用、不确定性、完成状态和附件详情默认折叠；会话操作通过菜单、Sheet、Dialog 和 AlertDialog 承载。所有用户可见文案默认中文，Kubernetes、MCP、API、SSE 等必要专有术语保留英文。

## User Stories

1. 作为 User，我想在导航中看到“AI 对话”，以便马上知道这是普通问答和只读运维辅助入口。
2. 作为 User，我想创建一个新的 AI 对话，以便把不同问题分开管理。
3. 作为 User，我想看到自己的历史 AI 对话，以便恢复之前的上下文。
4. 作为 User，我想手动重命名会话，以便用更容易识别的标题管理历史。
5. 作为 User，我想让系统根据第一条消息自动生成初始标题，以便无需手动整理每个会话。
6. 作为 User，我想置顶重要会话，以便它们始终出现在列表前面。
7. 作为 User，我想取消置顶，以便恢复普通排序。
8. 作为 User，我想归档暂时不用的会话，以便默认列表保持清爽。
9. 作为 User，我想恢复已归档会话，以便继续使用历史上下文。
10. 作为 User，我想永久删除会话并看到明确的确认提示，以便清理不再需要的内容。
11. 作为 User，我想删除会话时一并删除没有被 Investigation Handoff 引用的附件，以便避免留下孤立数据。
12. 作为 User，我想搜索自己的会话标题、消息内容、附件名称和已提取的附件文字，以便快速找回历史答案。
13. 作为 User，我想按正常、置顶和已归档状态筛选搜索结果，以便缩小范围。
14. 作为 User，我想编辑之前发送的问题并从那里继续，以便修正表述而不用覆盖原记录。
15. 作为 User，我想对一个 AI 回答执行“重新生成”，以便比较不同回答。
16. 作为 User，我想在多个回答分支之间切换，以便保留和比较每个版本。
17. 作为 User，我想看到原始消息不会被分支覆盖，以便保留完整上下文和 Handoff 引用关系。
18. 作为 User，我想拖拽、粘贴或选择文件，以便把实际运维材料交给 AI 对话。
19. 作为 User，我想在发送前看到文件名称、缩略图、上传进度和处理状态，以便知道附件是否准备好。
20. 作为 User，我想在发送前移除或重试失败附件，以便修正上传问题。
21. 作为 User，我想上传常见图片、PDF、日志、文本、Markdown、JSON、YAML 和 CSV，以便覆盖日常排障材料。
22. 作为 User，我想在不支持的文件类型、大小超限、解析失败或安全扫描失败时看到中文原因，以便知道下一步怎么处理。
23. 作为 User，我想让 AI 真正读取已支持的文件内容和图片，而不是只看到文件名，以便获得有用回答。
24. 作为 User，我想让附件内容被当作不可信资料处理，以便附件文字不会改变工具权限或执行授权。
25. 作为 User，我想只通过 Gateway 的鉴权接口下载自己的附件，以便文件不会被浏览器或外部存储直接暴露。
26. 作为 User，我想把当前分支中选定的消息和附件 Handoff 到 Incident，以便把问答结果交给事件调查继续处理。
27. 作为 User，我想在 Handoff 后选择进入事件调查或留在 AI 对话，以便控制自己的工作流。
28. 作为 User，我想知道 Handoff 内容仍然是 Human Input，不是 Evidence、Approval 或执行授权，以便理解治理边界。
29. 作为 User，我想在原 AI 对话被删除后仍保留已经 Handoff 的调查材料，以便调查记录不依赖聊天历史。
30. 作为 User，我想在桌面端折叠会话栏，在移动端通过侧滑面板打开，以便信息密度不会压满屏幕。
31. 作为 User，我想默认只看到回答正文，把工具活动、证据引用、不确定性和完成状态按需展开，以便先读懂结论。
32. 作为 User，我想看到所有按钮、菜单、状态、错误和空状态都使用中文，以便不需要猜英文含义。
33. 作为 Platform Administrator，我想通过 Gateway 的审计和权限边界管理 Chat Session 和附件，以便保留现有安全模型。
34. 作为 Platform Operator，我想在附件安全扫描不可用时看到可观测失败，而不是得到未扫描的文件，以便系统保持保守行为。
35. 作为 User，我想在断线后恢复会话列表、消息、附件状态和事件游标，以便网络波动不会导致重复发送或丢失状态。

## Implementation Decisions

### 产品语言与导航

- “AI 对话”只用于 Console 用户可见文案；领域术语、API、数据库和事件继续使用 `Chat Session`。
- 现有登录、同源访问、Session、RBAC、CSRF、幂等和 SSE 约束保持不变。
- shadcn/ui 是唯一通用 UI 组件系统；assistant-ui 的组件使用现有 shadcn primitive 和项目设计 token 组合。
- 使用已选的登录和侧栏 block；示例品牌、假数据、假链接和未接入的注册流程不进入生产界面。

### Chat Session Module

- Gateway 继续拥有 Chat Session、消息、事件游标、会话操作审计和附件引用。
- Chat Session 只属于创建它的 User；首期不做团队共享、公开链接或跨 User 转移。
- 会话默认长期保留，直到 User 主动删除；不再使用当前的 30 天自动过期作为产品行为。
- 删除是永久删除并需要 AlertDialog 确认。未被 Investigation Handoff 引用的 Chat Attachment 随会话删除；已被 Handoff 引用的材料由 Investigation 保留。
- 初始标题由第一条 User 消息生成；User 手动重命名后不再被自动标题覆盖。标题需限制长度并保持安全文本。
- 置顶会话优先显示，再按最近更新时间排序。归档会话从默认列表隐藏但可搜索、查看和恢复；归档会话自动取消置顶。
- 搜索只返回当前 User 有权访问的会话，覆盖标题、消息正文、附件名称和已提取文本，并支持正常/置顶/归档筛选。

### Message Branch Module

- 消息从线性列表扩展为不可覆盖的分支树；每个 User 编辑和每次 Assistant 重新生成都产生新的分支。
- 原消息、原回答、附件引用和 Handoff 选择的真实 ID 保持稳定。
- 分支切换只改变当前视图，不删除其他分支；运行中的消息不能切换或编辑。
- Handoff 只能引用当前可见分支中明确选定的消息，服务端重新校验消息归属、状态和权限。

### Chat Attachment Module

- 首期允许 PNG、JPEG、WebP、PDF、TXT、LOG、Markdown、JSON、YAML 和 CSV。
- 首期限额为每条消息最多 5 个附件、单个附件最多 20MB、单条消息总计最多 50MB。
- 不接受 Office 文档、压缩包、可执行文件、证书、kubeconfig、凭据文件或其他可直接改变执行环境的材料。
- 上传生命周期至少包括 pending、uploading、scanning、ready、rejected 和 failed；发送只能引用 ready 附件。
- Gateway 校验文件名、声明 MIME、真实文件签名、大小和数量；文件名不得参与路径拼接。
- 附件在进入模型前必须通过恶意文件扫描和受限解析。扫描器不可用、扫描失败或解析超限时拒绝模型使用。
- 文本内容执行已有敏感信息保护规则；疑似凭据、Token、密码和 Secure Input 不进入模型、日志、审计或搜索索引。
- PDF 和文本文件只产生有界的提取文本；图片只有在当前 Model Provider 已验证支持图片输入时才可发送给模型，否则发送前拒绝并说明原因。
- 附件内容明确标记为不可信 User context；它不能创建工具权限、Evidence、Approval、Execution Grant 或 Connector Command。
- 二进制文件保存在 Gateway 持久化卷，元数据、哈希、状态、引用关系和保留关系保存在 Gateway-owned 数据库。浏览器只能通过 Gateway 鉴权下载。
- 同一内容以哈希去重，但每个 Chat Session 和 Investigation Handoff 保留独立引用与权限；删除只清理没有保留引用的物理文件。
- 不使用 Assistant Cloud、浏览器直连对象存储、MinIO 或第二套 Chat persistence owner。

### Gateway HTTP and OpenAPI Contract

- 扩展当前 `/api/v1/chat` contract，增加会话重命名、置顶/取消置顶、归档/恢复、永久删除、搜索、附件上传/查询/下载/删除和分支操作。
- 所有写操作继续使用 creator-scoped CSRF、User ownership check、request ID 和 idempotency key；重复请求必须返回相同结果，冲突请求必须明确报错。
- Chat 事件流增加会话属性、分支和附件状态变化事件，继续使用现有 event cursor 和 SSE replay，不把它改成另一套浏览器状态机。
- OpenAPI、generated Console types、Gateway HTTP Adapter、直接消费方和 contract tests 同一变更更新。
- 删除、附件下载、附件扫描失败、分支切换和 Handoff 结果均不得泄露其他 User 的资源是否存在。

### assistant-ui Adapter

- 使用 `ExternalStoreRuntime` 将 Gateway 返回的消息、状态、扩展字段和 SSE 刷新映射到 assistant-ui；TanStack Query 和 Gateway 仍是服务端状态权威。
- 使用自定义 Thread List adapter 接入 Gateway 的会话列表、搜索、置顶、归档、恢复、重命名和删除，不使用 Assistant Cloud Thread persistence。
- 使用自定义 AttachmentAdapter 对接 Gateway 上传生命周期、进度、扫描状态、下载和删除。
- 使用 assistant-ui 的 BranchPicker、编辑、重新生成、Attachment、Message 和 Composer primitives；业务权限、Handoff、资源范围和重试请求保留在现有页面/Adapter。
- assistant-ui 不处理 Investigation Event、Approval、Evidence 或 Connector Command；Workbench 继续消费其现有 Gateway contract。

### Handoff and Investigation Feedback

- Handoff 复制选定消息和附件引用为 Investigation 的 Human Input 材料；不改变 Chat Session 与 Investigation 的独立生命周期。
- Handoff 返回目标 Incident 和 Investigation 标识，Console 提供“进入事件调查”链接和“留在 AI 对话”操作。
- Workbench 继续使用 assertion、correction、retraction、pause、takeover、terminate 和 reinvestigate 现有行为；本规格只优化入口、折叠、反馈提示和导航，不重写调查事件循环。

### Progressive Disclosure and Chinese UI

- 正文、发送状态、失败原因和下一步默认可见；工具活动、证据引用、不确定性、技能版本、附件解析详情和事件元数据默认折叠。
- 会话操作使用图标按钮、Tooltip、DropdownMenu；删除使用 AlertDialog；Handoff 和附件详情使用 Dialog/Sheet；移动端使用可关闭的侧滑面板。
- 用户可见英文全部改为中文，特殊术语保留英文并在首次出现时给出中文语境。代码标识符、路径、API 字段和错误码不改名。
- 所有状态必须提供 loading、empty、error、offline/reconnecting、上传中、扫描中、解析失败和权限拒绝视图。

## Testing Decisions

- 测试只验证公开 Module Interface 和 HTTP contract 的外部行为，不依赖私有 SQL 顺序、React 内部状态或 assistant-ui 内部实现。
- Chat Session Module 定向测试覆盖创建、重命名、置顶、归档/恢复、删除、搜索过滤、长期保留、分支树、消息 ID 稳定性、幂等冲突、ownership 和事件回放。
- Chat Attachment Module 定向测试覆盖 MIME spoofing、真实签名、路径穿越、大小/数量限制、扫描器拒绝和不可用、解析上限、敏感信息拒绝、去重、引用保留、删除清理和鉴权下载。
- Gateway contract 测试覆盖新增 OpenAPI schemas、成功/重复/冲突/未授权/越权/不存在/安全拒绝/扫描失败响应和 SSE 事件类型。
- Diagnosis contract 测试验证文本附件和图片附件以受限、不可信内容进入模型；附件不能增加工具能力、Evidence、Approval 或执行权限。
- Console Vitest 测试覆盖中文文案、会话操作菜单、折叠默认状态、Dialog/Sheet、上传状态、搜索筛选、分支切换、Handoff 成功态和 adapter 映射。
- Playwright 覆盖 `login → AI 对话 → 新建会话 → 重命名 → 置顶 → 搜索 → 归档/恢复 → 上传 → 分支 → Handoff → 进入事件调查` 主流程，并在桌面和移动视口验证无横向溢出和无重叠。
- 按项目测试策略先运行受影响 Module、直接 contract 和 Console 静态检查；只有跨模块 contract、迁移或发布验收时扩大范围。

## Out of Scope

- Assistant Cloud、`@assistant-ui/cloud-ai-sdk`、`react-o11y`、外部托管会话持久化和浏览器直连对象存储。
- Chat Session 与 Investigation 的双向消息同步；Handoff 仍是显式单向复制。
- Chat 内容自动升级为 Evidence、Approval、Execution Grant 或 Connector Command。
- 团队共享会话、公开分享链接、跨 User 转移、评论协作和实时多人编辑。
- Office 文档、压缩包、可执行文件、凭据文件、OCR、语音输入和语音播报。
- 向 Gateway、Diagnosis、Connector、MCP、Notification Engine 或 Console 增加新的进程边界。
- 恢复站/回收站、永久删除后的恢复、PostgreSQL、HA、备份和跨集群对象存储迁移。
- 复活 `.scratch/aiops-console-redesign/` 中已废弃的 PRD、旧 Console、旧 API、旧状态机或 legacy CSS。

## Further Notes

- 当前实现的 Chat Session 文件已经超过 500 行；实现前必须记录其 Module、公开 Interface 和定向 selector，不能继续把所有会话、附件和搜索逻辑堆进一个总 Store。
- 新能力应落入 Gateway-owned Chat Session、Message Branch 和 Chat Attachment Module；HTTP Adapter 只验证输入、鉴权、调用公开 Interface 和序列化。
- 附件扫描器是安全信任边界的一部分。若部署未提供可用扫描器，上传必须明确不可用，不能自动降级为未扫描上传。
- 下一步使用本规格拆分依赖有序的垂直票据；每张票都必须同时包含最小后端 contract、Console 使用路径和定向测试。
