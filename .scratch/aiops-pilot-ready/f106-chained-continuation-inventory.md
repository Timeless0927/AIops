Type: task-record
Status: done

# F106 链式 Continuation mutation inventory

## 目标

修复 `adopt_existing` ledger 再次失败时只报告本轮 mutation、遗漏上一 signed
Deployment Continuation 已核对 mutation 的问题，并在 effect 前拒绝跨 ledger 重用
operation ID。

## Module record

- Owner：Acceptance Evidence Module。
- 公开 Interface：`AcceptanceEvidence.bind_operation`、
  `AcceptanceEvidence.failure_summary`；load-time validation 仍由
  `AcceptanceEvidence.open` 执行。
- 定向 selector：`tests/test_pilot_acceptance_deployment_continuation.py`；直接
  consumer：`tests/test_pilot_acceptance_gate_reuse.py`。
- `aiops/acceptance/evidence.py` 从 795 行增至 800 行，仍只负责 immutable ledger、
  operation journal 与公开 failure summary；本次能力属于该 owner，不拆出转发
  helper 或第二个 inventory owner。
- `tests/test_pilot_acceptance_deployment_continuation.py` 从 477 行增至 494 行。

## Verification

- 定向 continuation/gate-reuse：32 passed。
- Acceptance workspace 与 package：344 passed。
- Python compile、reviewed-range `git diff --check`：passed。
- Standards/Acceptance Contract 最终审查在 F106 freeze 前重新执行。
