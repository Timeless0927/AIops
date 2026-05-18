# 变更请求流程

当用户反馈会改变行为、范围、数据、状态机、权限、集成、部署或验收标准时，必须使用本流程。

## 状态值

- `draft`：已记录，尚未初筛。
- `needs-product-review`：需要 `product-domain-agent` 评审产品、BDD 或 DDD 影响。
- `needs-architecture-review`：需要 `architect-agent` 评审 SDD 影响。
- `approved`：所有必要评审已批准。
- `in-progress`：已开始实现。
- `done`：已实现、测试、验证并同步文档。
- `rejected`：已拒绝。
- `deferred`：有效但不进入当前范围。

## 默认负责人

`dev-lead-agent` 是所有 CR 的入口负责人。

## 影响分流

| 影响内容 | 最终评审者 |
| --- | --- |
| 产品目标、范围、非目标、优先级 | `product-domain-agent` |
| BDD 场景、用户流程、验收结果 | `product-domain-agent` |
| DDD 子域、限界上下文、聚合、不变量、领域事件 | `product-domain-agent` |
| 模块边界、API、事件契约、数据库、部署、安全、可观测性 | `architect-agent` |
| 任务拆分、测试、代码实现、bug 修复 | `dev-lead-agent` |

## CR 模板

```markdown
## CR-YYYY-MM-DD-NNN：简短标题

状态：draft
负责人：dev-lead-agent
创建日期：YYYY-MM-DD

### 用户反馈

原始请求或观察到的不符合预期之处。

### 初筛影响

- PDD：无 / 可能 / 必须
- BDD：无 / 可能 / 必须
- DDD：无 / 可能 / 必须
- SDD：无 / 可能 / 必须
- TDD：无 / 可能 / 必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要 / 需要 / 已批准 / 已拒绝
- architect-agent：不需要 / 需要 / 已批准 / 已拒绝

### 决策

接受 / 拒绝 / 延后，并写明原因。

### 影响文件

- docs/...
- app/...
- tests/...

### 验收标准

- [ ] 可观察行为或验证结果。

### 验证

命令或人工检查结果。
```

## 完成门禁

CR 只有满足以下条件才能标记为 `done`：

- 必要评审已完成。
- 受影响的 PDD/BDD/DDD/SDD 文档已更新。
- 已记录测试或替代验证。
- `docs/TODD.md` 已更新。
- 功能状态变化时，`docs/development-progress.md` 已同步。
