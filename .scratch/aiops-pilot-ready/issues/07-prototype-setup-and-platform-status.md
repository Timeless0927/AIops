Type: prototype
Status: resolved
Blocked by: 03 (Define Web Setup And Integration Ownership), 04 (Define Bundled Observability And Alert Path), 05 (Define Model And Notification Readiness)

# Prototype Setup And Platform Status

## Question

What should the optional first-run setup flow and persistent top-level Platform Status experience look and behave like so a Platform Administrator can configure or skip integrations, understand real readiness, recover from partial setup, and then enter the Incident workspace without mistaking missing dependencies for an empty healthy platform?

The prototype must cover desktop and narrow layouts, resumability, skipped and failed steps, dependency health, access control, re-entry from regular navigation, and the relationship between Platform Status and the existing `/admin` capabilities. It is a configuration workflow, not a tutorial tour.

## Answer

采用持久化顶层 `Platform Status` 页面，以“能力轨 + 当前能力详情”为主信息架构。能力轨始终列出 Model Provider、Notification Destination、Connector/Cluster 和 Observability，不因某项健康或被跳过而隐藏；窄屏改为可横向浏览的能力轨。页面使用每项能力独立的 `readiness`、configuration、verification、availability 和 bounded reason，而不是聚合出全局 `setup complete`。

首装只是 Platform Administrator 第一次进入同一 Platform Status 页时的状态，不建立强制首屏、一次性 wizard 或浏览器端 setup 状态机。管理员可随时离开进入 Incident workspace，再从常规顶层导航返回继续配置、验证、重试或跳过 optional capability。`skipped` 必须显示为“已跳过 · 未就绪”，不能计入 ready；required capability 不提供 skip。

Incident workspace 在所有状态下始终可进入。缺失能力在对应诊断、证据或通知动作处继续显示限制，不能伪装成空的健康平台。Platform Administrator 可看到配置和验证动作；普通 User 只看到不含 secret、endpoint、credential 或 provider raw error 的安全摘要，没有 configure/test 控件。

Platform Status 负责跨能力 readiness 总览、失败原因和恢复入口；`/admin` 继续负责用户、权限、集群、资源目录、通知目的地等领域详情。两者不是替代关系，Platform Status 的“详细管理”进入 `/admin` 对应领域，而不复制管理表单。

原型在 `/prototype/setup-status`，通过 URL 参数 `variant=rail|queue|matrix` 与 `state=partial|ready|failed|readonly` 对比三种结构和四种状态。评审选择 capability rail；recovery queue 会隐藏健康项并把 setup 误表达为有限流程，不作为主导航；status matrix 信息密度适合以后有真实日常需求时作为辅助视图，本轮不承诺实现。

原型交互状态只在内存中，用于验证 configure -> needs verification -> ready、retry 和 skip 行为，不定义 API、持久化 schema、reason code 或产品文案 contract。桌面和 `390px` 窄屏检查均无页面级横向溢出或业务内容遮挡；readonly 场景没有 mutation 控件。
