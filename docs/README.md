# AIOps Backend 文档入口

最后对齐日期：2026-07-09

本目录只保留当前后端仓库仍需要维护的长期文档。前端源码和前端产品计划已迁到 `/root/AIOPS-WEB`；本仓库文档只记录 Gateway、diagnosis service、Connector、MCP、部署和后端 API 边界。

## 当前文档

| 文档 | 用途 |
| --- | --- |
| [当前架构](current-architecture.md) | 当前后端服务、数据流和部署边界。 |
| [架构图集](architecture-diagrams.md) | 当前 split-service 后端 Mermaid 图。 |
| [Notification Center 与 Feishu](notification-center-feishu.md) | Gateway-owned notification-only 通道。 |
| [V1 功能测试矩阵](v1-functional-test-matrix.md) | 当前只读 diagnosis 链路的测试覆盖。 |
| [用户手册](user-guide.md) | 部署、验证、Gateway API 和操作边界。 |
| [ADR-0002](adr/0002-v1-repository-boundaries.md) | apps、contracts、runtime/toolsets 的后端仓库边界决策。 |

任务状态、验收结论、阻塞项、PR、commit 和剩余风险仍以 Multica issue 为准。
