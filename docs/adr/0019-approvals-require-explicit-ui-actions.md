# Mutation approval requires an explicit UI action

Status: accepted

Human Input supplied through chat, notes, questions, notifications, or natural-language statements can affect an Investigation but can never authorize a Cluster mutation. Approval exists only when an eligible User manually acts on a dedicated approval control showing the frozen Recommended Action. Gateway rechecks Approval Authority at that moment and records a distinct Approval and audit event. Console presents Human Input as “待补充信息” and Approval as “待审批动作”; it never infers approval from text, and Feishu remains notification-only.
