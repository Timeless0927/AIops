# AIOps Monorepo 文档入口

最后对齐日期：2026-07-12

本目录保存 AIOps monorepo 的长期文档。Gateway、Diagnosis、Connector、MCP 与 Console source 共用一个仓库；各运行进程、Console OCI image 和部署边界仍然独立。

## 当前文档

| 文档 | 用途 |
| --- | --- |
| [当前架构](current-architecture.md) | 当前服务、Console、数据流和部署边界。 |
| [架构图集](architecture-diagrams.md) | 当前 split-service 与 Console Mermaid 图。 |
| [V1 功能测试矩阵](v1-functional-test-matrix.md) | 当前只读 diagnosis 链路的测试覆盖。 |
| [用户手册](user-guide.md) | 部署、验证、Gateway API 和操作边界。 |
| [ADR-0002](adr/0002-v1-repository-boundaries.md) | apps、contracts、runtime/toolsets 的后端仓库边界决策。 |
| [ADR-0052](adr/0052-monorepo-source-independent-console-artifact.md) | Monorepo source 与独立 Console artifact 决策。 |
| [ADR-0054](adr/0054-single-command-pilot-release-installation.md) | 单命令 Kustomize Pilot Release 安装、bootstrap credential、NodePort 与存储决策。 |
| [ADR-0055](adr/0055-owner-held-web-setup-state.md) | Web Setup、integration owner、verification、secret 与多 Cluster Connector lifecycle 决策。 |
