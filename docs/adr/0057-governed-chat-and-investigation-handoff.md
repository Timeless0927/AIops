---
status: accepted
---

# 通用 Chat 与受治理 Investigation 保持独立

通用 Chat Session 与 Incident Investigation 是不同的产品对象：Chat 提供用户权限范围内的只读问答，Investigation 承载 Evidence、判断、治理和后续 Approval。User 必须显式执行 Investigation Handoff，才能把选定聊天内容作为 Human Input 关联到现有 Incident，或为没有 Alert Signal 的真实运维问题创建 User-created Incident；聊天内容不会因此成为 Evidence 或执行授权。

该能力沿用现有 Console、Gateway、Diagnosis 和 MCP 进程边界，不增加独立 Chat Agent 服务。Gateway 拥有 Chat Session、授权、持久状态和审计，Diagnosis 执行 Chat 与 Investigation 共用的受限工具循环，MCP Integration 提供管理员允许的只读工具。用户看到结构化 Decision Trace，而不是模型原始 Chain of Thought；任何 Kubernetes mutation 继续只能经 Investigation、Evidence Gate、Approval 和 Execution Grant。
