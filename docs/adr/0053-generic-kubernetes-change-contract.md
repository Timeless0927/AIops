# 通用 Kubernetes Change 取代固定 remediation action

Status: accepted

AIOps 需要让持有显式 Authority 的 User 管理整个注册 Cluster，包括系统 namespace、cluster-scoped resources 和 CRD；逐项新增 `restart_deployment`、`create_job` 等 action 无法覆盖该目标。User 只提交自然语言 Change Request，模型生成 immutable Change Plan，Gateway 通过 live precondition、policy 和 API Server dry-run 冻结 exact diff，User 显式 Approval 后，Connector 才能使用单次 Execution Grant 执行 canonical create、RFC 6902 JSON Patch 或 preconditioned delete。自由 YAML、shell、kubectl argv、自动 mutation retry 和模型授权均被禁止。

Connector 是唯一 Kubernetes API Adapter，并独占专用广权限 `aiops-change-executor` ClusterRole；其他 AIOps 进程不持有 Kubernetes credential。该能力选择了结构化应用层授权而非逐 namespace RBAC opt-in，因此 Connector credential compromise 可能影响整个 Cluster。Projected token、NetworkPolicy、fresh auth、分级 Approval Authority、exact hash/precondition、server-side dry-run、加密 sensitive journal、post-check、Unknown Outcome reconciliation 和 durable audit 用于降低风险，但不掩盖或消除这个 accepted trade-off。
