# PRD：Console Chat 会话状态 Module

状态：ready-for-agent

## Problem Statement

Console 的 AI 对话页面同时负责页面展示、Chat Session 查询与变更、SSE 事件处理、TanStack Query 缓存更新、请求取消和派生状态计算。新增会话能力时，维护者必须在一个页面 Module 中同时理解展示与服务端状态规则，改动容易影响无关交互，测试也需要装配完整页面才能覆盖核心行为。

## Solution

在不改变用户界面、Gateway contract 和用户可见行为的前提下，把 Chat Session 的服务端状态编排迁入一个内聚的会话控制 Module。页面继续拥有 URL 导航和临时 UI state；Gateway 继续拥有真实 Chat Session 与 Chat Message 状态；新 Module 只协调现有 TanStack Query、mutation、SSE 和缓存规则。

## User Stories

1. 作为 User，我希望 AI 对话页面在内部整理后仍能正常创建、选择和查看 Chat Session，从而不受架构调整影响。
2. 作为 User，我希望发送、取消、重试、编辑和重新生成消息的行为保持不变，从而不需要重新学习操作方式。
3. 作为 User，我希望 SSE 断线重连和增量回答继续正确显示，从而不会看到重复、回退或丢失的消息内容。
4. 作为 User，我希望消息分支切换和 Investigation Handoff 保持原有权限、幂等和导航行为，从而继续完成现有工作流。
5. 作为 User，我希望搜索、筛选、资源选择和弹窗状态仍只影响当前页面交互，从而不会被错误持久化为服务端状态。
6. 作为 Maintainer，我希望 Chat Session 的请求、变更、事件和缓存规则集中在一个 Module，从而修改一条规则时不必遍历整个页面实现。
7. 作为 Maintainer，我希望页面不接触原始 QueryClient 和 mutation 状态，从而减少页面与数据访问实现的耦合。
8. 作为 Maintainer，我希望能通过会话控制 Module 的公开 Interface 验证核心行为，从而不必为每条状态规则渲染完整页面。
9. 作为 Maintainer，我希望行为保持迁移和后续功能变化分开交付，从而可以独立审查和回滚。
10. 作为 Maintainer，我希望现有 Chat 页面和浏览器验收继续通过，从而证明迁移没有改变用户可见行为。

## Implementation Decisions

- 新增一个 React hook 形式的 Chat Session 控制 Module；不新增 Context、class、factory、Redux、Zustand 或外部依赖。
- Module 接收当前 Chat Session identity、搜索词和筛选条件，返回页面需要的服务端状态、派生状态和用户动作。
- Module 不向调用方暴露原始 QueryClient、query object 或 mutation object。
- Router 继续拥有当前 Chat Session identity 和页面跳转；创建会话后由页面根据返回的 identity 完成导航。
- 页面继续拥有搜索词、筛选条件、选中资源、弹窗和其他临时 UI state。
- Gateway 继续拥有真实 Chat Session、Chat Message、分支、Handoff 和事件 cursor；浏览器不建立第二套状态机。
- 保留现有事件规则：message delta 可以更新当前缓存，其他事件触发已有查询失效和重新获取。
- 保留现有取消、错误优先级以及 loading、busy、generating、connection 派生语义。
- 第一轮保持现有 ChatView Interface 和用户界面，不同时重做展示结构。
- 第一轮不迁移已经独立存在的 Attachment Adapter，也不改变附件上传、删除或重试行为。
- 行为保持迁移完成时删除页面中的旧实现，不保留双路径或兼容开关。

## Testing Decisions

- 迁移前复用现有最小 characterization test，锁定 SSE 增量、取消和缓存更新的现有可观察行为；已有覆盖时不复制测试。
- Chat Session 控制 Module 通过公开 Interface 测试状态和动作，不断言私有调用顺序。
- 现有 Chat runtime 和页面测试继续验证 Gateway message 映射、Thread List、页面展示和错误状态。
- 现有浏览器测试继续验证发送、取消、会话管理、消息分支和 Investigation Handoff。
- 运行 Console TypeScript no-emit 和 production build，验证直接消费方和构建 contract。
- 不以全量仓库测试作为局部迁移的默认验证方式。

## Out of Scope

- 改变 Console 的视觉设计、文案或交互流程。
- 修改 Gateway、Diagnosis、OpenAPI 或 SSE contract。
- 实现真正中断正在运行的模型调用。
- 优化 Gateway SSE 轮询或 SQLite 并发。
- 拆分总 HTTP client Module。
- 重构 Attachment Adapter 或附件展示。
- 新增 AI 对话功能、状态管理依赖或浏览器端持久化状态机。

## Further Notes

- 这是行为保持的责任迁移，不是功能票。
- 同一 Chat 页面上的普通新需求和非紧急 bug 在迁移完成后实施；生产故障、安全问题和数据风险仍应立即优先处理。
- 已完成的 AI 对话 T01-T09 保持不变，本规格单独记录后续架构治理。
- 体量门禁：`apps/aiops_console_web/src/chat/chat-page.tsx` 开始时 708 行，所属 Console Chat Module，公开 Interface 为路由入口 `ChatPage` 和展示入口 `ChatView`；本次把完整 Chat Session server-state 编排迁入 `useChatSessionController`，结束时页面为 575 行。定向 selector 为 `src/chat/chat-runtime.test.ts`、`src/chat/chat-page.test.tsx`、`src/api/client.test.ts`、`e2e/console-chat-runtime.spec.ts`、`e2e/console-chat-management.spec.ts`、`e2e/console-chat-branches.spec.ts` 和 `e2e/console-chat-handoff.spec.ts`。
