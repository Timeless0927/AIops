# ADR-0006: Console Next PRD 是当前产品事实源

Date: 2026-07-08

Status: Accepted

Console Next 的新实现以 `docs/aiops-console-next-plan.md` 为唯一当前产品事实源。`aiops-console-v1-contract.md`、`current-architecture.md`、`user-guide.md` 和既有代码只反映历史阶段或当前落地状态；当它们与 Console Next 冲突时，冲突项进入差距审计和迁移清单，不再反向修改 Console Next 目标。
