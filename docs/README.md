# AIOps 文档入口

最后对齐日期：2026-06-17

本目录只保留当前 AIOps 架构下仍应进入 GitHub 留档的长期文档。Multica issue 仍是任务状态、验收结论、阻塞项、PR、commit 和剩余风险的事实源；仓库文档只记录稳定架构、契约、图示和操作指南。

## 当前文档

| 文档 | 用途 |
| --- | --- |
| [当前架构](current-architecture.md) | 最新系统边界、服务职责、关键流程和路线图状态。 |
| [架构图集](architecture-diagrams.md) | 从 AIO-73、AIO-87、AIO-80、AIO-86、AIO-95 和当前代码中整理出的 Mermaid 图。 |
| [Issue 设计归档](issue-design-archive.md) | 从历史 issue 中沉淀出的稳定产品和架构决策。 |
| [Console V1 契约](aiops-console-v1-contract.md) | AIO-87 输出的 Gateway-only Console 信息架构和 API 交接契约。 |
| [Notification Center 与 Feishu](notification-center-feishu.md) | Gateway-owned Notification Center 与 Feishu notification-only 契约。 |
| [V1 功能测试矩阵](v1-functional-test-matrix.md) | 当前 split read-only diagnosis 链路的回归和 smoke 覆盖。 |
| [用户手册](user-guide.md) | 当前部署、验证和操作边界的快速指南。 |
| [ADR-0002](adr/0002-v1-repository-boundaries.md) | apps、contracts、legacy compatibility 的仓库边界决策。 |

## 已删除的过期文档

旧 `00-PDD` 到 `05-TDD`、`CHANGE-REQUESTS`、`TODD`、`development-progress`、`hermes-sre-agent-*`、`feishu-sre-agent-*`、`ADR-0001` 和 `docs/superpowers/*` 描述的是更早的 Feishu 原生审批和 all-in-one Hermes/Feishu SRE Agent 路线。该路线与当前 AIO-73 架构冲突：Gateway/control-plane 拥有 approval 状态，Feishu 仅 notification-only，split services 通过 Gateway、Connector、MCP 和共享契约协作。

历史执行证据仍保留在 Multica issue 和 Git 历史中。除非新的 issue 明确改变架构，不要把已删除文档重新作为当前事实源引入。
