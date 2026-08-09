# Tickets：Console 领域 Client

按 [Console 领域 Client 规格](./PRD.md) 把总 HTTP Client 迁入真实领域 owner，并保留一个窄的共享 transport。

Work the **frontier**：任何 blockers 已全部完成的票都可以开始；C02 与 C03 在 C01 后彼此独立，C04 负责最终 contract。

## C01 提取共享 transport 与认证 Client

**What to build:** User 的登录、退出、身份恢复和 fresh authentication 行为保持不变，同时后续领域 Client 可以复用唯一的请求、CSRF、request identity 和错误归一化 Interface。

**Blocked by:** None — can start immediately.

- [x] 共享 transport 只暴露现有领域 Client 所需的 request、CSRF write、request identity 与 Gateway error normalization。
- [x] Actor、登录、退出和 fresh authentication 进入认证 owner，所有直接调用方改用新 Interface。
- [x] 总 Client 改为消费共享 transport，已迁移的认证实现从总 Client 删除，不增加 re-export 或第二套 transport。
- [x] 现有 request identity fallback、Gateway error 和认证 characterization tests 迁入可独立运行的 selector。
- [x] 受影响 Vitest、TypeScript no-emit 与 production build 通过。

## C02 迁移 Chat 与 Incident/Investigation Client

**What to build:** User 可以继续使用 Chat Session、附件、消息分支、Investigation Handoff、Incident 和 Investigation 工作流，而这些请求与类型由对应领域 Module 拥有。

**Blocked by:** C01 提取共享 transport 与认证 Client.

- [x] Chat Session、Message、Attachment、Branch 与 Handoff 请求和类型进入 Chat Client，Chat 页面与 controller 不再从总 Client 获取这些能力。
- [x] Incident list/workbench 与 Investigation events、Human Input、control 和 reinvestigation 请求及类型进入 Incident/Investigation Client。
- [x] Chat 对 Incident 的依赖直接使用 Incident owner；尚未迁移的 Resource 能力可以在 C03 前继续由总 Client 拥有。
- [x] 已迁移实现和 characterization tests 从总 Client 与总测试删除，不保留兼容 re-export。
- [x] Chat、Incident/Investigation 定向 Vitest、TypeScript no-emit、production build，以及现有 Chat runtime、management、branches、handoff Playwright 通过。

## C03 迁移 Change、Report、Resource 与 Platform Client

**What to build:** User 可以继续读取和操作 Change Request、Incident Report、Resource Workspace 与 Platform Status，而每个能力由所属领域 Client 提供并可独立验证。

**Blocked by:** C01 提取共享 transport 与认证 Client.

- [ ] Change Center、Change Request、Secure Input、Phase Approval/Execution 与 reconciliation 请求和类型进入 Change Client。
- [ ] Incident Report library、draft、update 和 publish 请求及类型进入 Report Client。
- [ ] Resource Workspace 请求和类型进入 Resource Client，Chat 直接消费该 owner。
- [ ] Platform Status 与 setup decision 请求和类型进入 Platform Client；其跨领域验证动作直接调用 Admin 或 Notification owner，不增加 Platform facade。
- [ ] 已迁移实现和 characterization tests 从总 Client 与总测试删除，不保留兼容 re-export。
- [ ] Change、Report、Resource、Platform 定向 Vitest、TypeScript no-emit、production build与现有 Platform Status Playwright 通过。

## C04 迁移 Admin 与 Notification Client 并删除总 Client

**What to build:** Platform Administrator 可以继续管理身份、Connector、Resource Catalog、Model Provider、MCP、Skill、Authority 和 Notification，同时 Console 不再暴露总 Client 或总测试入口。

**Blocked by:** C02 迁移 Chat 与 Incident/Investigation Client; C03 迁移 Change、Report、Resource 与 Platform Client.

- [ ] User/Team/Role、Connector Enrollment、Resource Catalog、Model Provider、MCP、Skill、Authority 与 audit 请求和类型进入 Admin Client。
- [ ] Destination、Route、Template、Noise Control、Silence、Delivery 与 redelivery 请求和类型进入 Notification Client。
- [ ] AdminPage 和 Platform Status 继续直接组合真实 owner Client，fresh authentication、reason、request identity 与 invalidation 行为保持不变。
- [ ] 旧总 Client 与总测试文件删除，仓库不再存在对旧入口的 import、re-export、兼容开关或重复实现。
- [ ] Admin 与 Notification 定向 Vitest、全部 Console Vitest、TypeScript no-emit、production build、现有 Chat Playwright 和 Platform Status Playwright 全部通过。
- [ ] 固定比较基准上的 Standards 与 Spec 双轴 review 均无阻塞问题。
