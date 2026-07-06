# Console approval center

## Goal

Add a Gateway-backed approval center so operators can review approval requests
and approve or reject them from Console.

## Position

- Order: 2
- Priority: P2
- Parallel: can start after incident workbench API assumptions are clear
- Depends on: Gateway approval request APIs
- Related backend task: controlled-mutation-execution

## Confirmed Facts

- Gateway owns approval state; Feishu is notification-only.
- Gateway already has approval request storage and list/detail routes.
- Execution is not part of this task.

## Requirements

1. List approval requests with status, risk, incident, action summary, requester,
   approver, and expiry.
2. Show approval detail with evidence refs, rollback plan, audit refs, and scope.
3. Provide approve/reject controls only when Gateway exposes safe decision APIs.
4. Fail closed: unavailable or unauthorized decisions must not mutate UI state as
   if they succeeded.
5. UI text must be Chinese.

## Acceptance Criteria

- [ ] Pending/approved/rejected/expired approvals are visible.
- [ ] Detail view shows enough context for a human decision.
- [ ] Approve/reject path is tested if Gateway route exists; otherwise task
      records the missing route and ships read-only center first.
- [ ] Frontend build and targeted tests pass.

## Out of Scope

- Mutation execution after approval.
- Feishu as approval source of truth.
