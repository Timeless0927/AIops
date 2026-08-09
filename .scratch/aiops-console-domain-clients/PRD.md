# PRD：Console 领域 Client

状态：ready-for-agent

## Problem Statement

Console 的 HTTP Adapter 目前集中在一个总 Client 中。认证、Chat Session、Incident、Investigation、Change Request、Incident Report、Resource Catalog、Platform Status、平台管理和 Notification 的类型与请求函数共享同一个公开 Interface。任一领域契约变化都可能触碰无关调用方，测试也无法按领域独立验证 Client 行为。

## Solution

在不改变 Gateway contract、用户界面和用户可见行为的前提下，把请求函数和领域类型迁入所属 Console Module。生成的 OpenAPI schema 继续作为唯一契约源；共享 transport 只负责 HTTP 请求、CSRF write、请求 identity 和 Gateway 错误归一化。聚合页面直接调用真实 owner Client，不增加页面专用 facade 或兼容 barrel。

## User Stories

1. 作为 User，我希望登录、退出和身份恢复行为保持不变，从而不受内部 Client 迁移影响。
2. 作为 User，我希望 Chat Session、附件、分支和 Investigation Handoff 继续按现有规则工作，从而可以继续完成对话工作流。
3. 作为 User，我希望 Incident、Investigation、Change Request、Incident Report 和 Resource Workspace 的读取与操作保持不变。
4. 作为 Platform Administrator，我希望 Platform Status、管理命令和 Notification 配置继续使用既有鉴权、fresh authentication、CSRF 和 request identity 语义。
5. 作为 Maintainer，我希望每个领域 Client 只暴露所属页面完成业务所需的类型和请求函数，从而让契约变化有明确 owner。
6. 作为 Maintainer，我希望所有领域 Client 复用一个窄的 transport Interface，从而只在一处维护请求 header、错误和 CSRF 规则。
7. 作为 Maintainer，我希望聚合页面直接组合真实 owner Client，从而不建立第二个总 Client 或页面专用转发层。
8. 作为 Maintainer，我希望能按领域运行 Client 测试，从而无需装配无关页面或完整 Console。
9. 作为 Maintainer，我希望迁移可以分批提交且每批保持 Console 可编译、可测试，从而便于独立审查和回滚。
10. 作为 Maintainer，我希望迁移完成后旧总 Client 和总测试文件被删除，从而不留下双 owner、兼容路径或继续扩张的入口。

## Implementation Decisions

- 这是行为保持的责任迁移；Gateway route、OpenAPI schema、请求与响应含义、错误语义、UI 和缓存规则保持不变。
- 生成的 OpenAPI schema 和共享 transport 保留在 API 基础设施 Module；transport 只拥有 HTTP request、CSRF write、request identity 和 Gateway error normalization。
- 认证、Chat、Incident/Investigation、Change、Report、Resource、Platform、Admin 和 Notification Client 与所属领域 Module 共置。
- 领域类型别名跟随其 owner Client；调用方不直接重新解释生成 schema。
- 聚合页面可以直接导入多个真实 owner Client；不增加页面 facade、Context、class、factory、registry 或第二套请求库。
- Chat 可以直接消费 Incident 和 Resource Client；Platform 可以直接消费 Admin 和 Notification Client，不复制这些 owner 的命令。
- 迁移分四批完成。每批删除已迁移的旧实现和测试；最终删除总 Client 与总测试文件，不保留 re-export barrel、兼容开关或双实现。
- 当前 Incident 页面所在的 prototype 目录不在本次迁移中改名；新 Incident Client 使用稳定领域归属，现有页面只更新 import。
- AdminPage 的查询、reason、fresh authentication、mutation 和 invalidation 状态治理不在本规格内改变。
- 不增加依赖、配置或未被当前调用方使用的扩展点。

## Testing Decisions

- 复用现有 Client characterization tests 锁定 request identity fallback、Gateway error normalization、CSRF write、Chat、Change、Admin 和 Notification 请求行为。
- 总 Client 测试按领域迁移到对应 Module；测试公开请求函数的可观察 fetch contract，不断言私有 helper 调用顺序。
- 每批运行受影响领域的 Client 与页面 Vitest selector，并运行 TypeScript no-emit 和 production build 验证直接消费方。
- Chat/Incident/Resource 批次运行现有 Chat runtime、management、branches 和 handoff Playwright；Platform/Admin/Notification 批次运行现有 Platform Status Playwright。
- 最终运行全部 Console Vitest、TypeScript no-emit、production build 和上述现有 Playwright selector。
- 不因纯迁移复制已有案例，不把全量仓库测试作为本地 Console 重构的默认验证方式。

## Out of Scope

- 修改 Gateway、OpenAPI 或任何 `/api/v1`、`/auth`、SSE contract。
- 改变 UI、文案、导航、TanStack Query cache key 或用户交互。
- 收拢 AdminPage 的管理命令上下文。
- 改名或重构 prototype 页面、Chat Session controller、Attachment Adapter 或其他页面状态 Module。
- 增加新的功能测试框架、请求依赖、客户端状态管理或兼容层。

## Further Notes

- 当前总 Client 为 699 行，总测试为 410 行，43 个生产或测试文件直接导入其公开 Interface。
- 所属 Console HTTP Adapter 能力的公开 Interface 是共享 transport 与各领域 Client 的导出函数和类型。定向 selector 由各票记录，最终验收覆盖全部 Console Vitest、TypeScript no-emit、production build、Chat Playwright 和 Platform Status Playwright。
- 本规格不改变领域术语，也没有满足 ADR 门槛的难逆转决策，因此不修改领域词汇表或新增 ADR。
