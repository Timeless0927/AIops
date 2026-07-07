# Console Next action grants, approvals, execution, and locks

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver the end-to-end mutation control path: chat/action proposal -> Gateway
structured Action -> policy classification -> approval or policy grant ->
one-time frozen execution grant -> lock -> preflight -> mutation -> post-check
-> timeline and audit.

The completed slice should be demoable as: operator requests a service restart,
Gateway creates a frozen action requiring approval, approver approves, Gateway
automatically executes the mock/allowlisted mutation once, and audit shows the
responsibility chain.

## User Stories Covered

- Parent stories 6, 7, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, and 38.

## Requirements

- Gateway converts recognized intents into structured Actions.
- Mutation targets are never guessed. Missing or ambiguous targets are
  clarified before approval creation.
- All changes use Action -> policy -> grant -> execution -> audit.
- Gateway is final risk classifier. Agent suggestions are not authoritative.
- First implementation supports Kubernetes read, Kubernetes allowlisted
  mutation, and notification.
- Approval is created automatically when policy requires it.
- Approval detail shows evidence, target, risk, preflight, rollback plan,
  expected impact, requester, agent, and responsibility fields.
- Reject requires a remark. Approve remarks follow policy.
- Self-approval is allowed only when policy permits and is audited.
- Approval pending timeout is enforced.
- If no approver is available, approval becomes `blocked:no_approver` and admin
  notification is created.
- Every mutation uses a one-time grant bound to frozen action hash and target
  scope.
- Grant consumption happens on success, failure, and preflight failure.
- Idempotency replays the same execution and does not authorize a new one.
- Approval-passed triggers automatic Gateway execution of the frozen action.
- Execution order is preflight -> mutation -> post-check.
- Post-check failure marks rollback required.
- Mutations on the same target require a lock.
- Global agent, per-cluster mutation, and target lock limits are configurable or
  policy-backed.

## Acceptance Criteria

- [ ] Structured Action creation rejects missing or ambiguous mutation targets.
- [ ] Gateway policy decides allow, block, or approval-required.
- [ ] Approval request includes frozen action payload/hash and responsibility
  fields.
- [ ] Approve/reject flows enforce remarks, timeout, eligibility, and
  self-approval policy.
- [ ] No-approver state is visible and notifies admins.
- [ ] Approval creates exactly one human approval grant.
- [ ] Policy auto-allowed mutation creates exactly one policy grant.
- [ ] Automatic execution runs preflight, mutation, and post-check.
- [ ] Grant consumption and idempotency are enforced.
- [ ] Target locks prevent conflicting concurrent mutations.
- [ ] Timeline and audit show policy decision, approval, lock, execution, and
  result.
- [ ] Tests cover approval state, action hash, grants, idempotency, locks,
  preflight failure, mutation failure, post-check failure, and rollback-required.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.
- Console Next five-role RBAC and user management.
- Console Next settings versions and policy management.
- Console Next conversations, agent runs, and SSE timeline.

## Further Notes

This is the core safety slice. Do not add new execution backends beyond the
first implementation set unless the parent PRD is revised.
