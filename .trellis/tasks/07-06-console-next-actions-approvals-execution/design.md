# Console Next actions/approvals/execution design

## Scope

This slice delivers the production-change safety path: structured actions,
policy, grants, approvals, locks, execution, timeline, and audit.

## Architecture

- Reuse the existing Gateway approval and execution path:
  - `approval_service.py` owns approval request state.
  - `approval_execution_service.py` owns execution idempotency and
    preflight/mutation/post-check records.
  - `settings_service.classify_action` owns policy decisions.
  - Connector `kubectl_executor.py` owns final command allowlist validation.
- Add the missing Gateway-owned structured action layer as the small owner of:
  - intent/action normalization
  - frozen action hash
  - policy grant records
  - target lock records
  - approval payload construction for recognized mutation actions
- Keep browser calls Gateway-only. Console can request/inspect actions and
  approvals; it never builds Connector envelopes or calls Connector directly.

## Data flow

1. Console or agent-run message posts an action request to Gateway.
2. Gateway normalizes the request into one frozen action payload.
3. Gateway rejects missing or ambiguous mutation targets before policy.
4. Gateway calls `settings_service.classify_action`.
5. `approval_required` creates an approval request with the frozen action hash
   and responsibility fields.
6. `policy_grant` creates one grant and may execute automatically through the
   same execution service.
7. Approval approval creates one human grant and automatic Gateway execution.
8. Execution claims a target lock, runs preflight -> mutation -> post-check,
   records timeline/audit, consumes the grant, and releases the lock.

## Confirmed decisions

- First mutation batch supports only Kubernetes allowlisted actions:
  - `restart_deployment`
  - `scale_deployment`
  - `rollback_deployment`
- Do not implement host execution or custom webhook execution in this slice.
- Agent may suggest risk, but Gateway is the final risk classifier.
- Mutation targets must not be guessed.
- Frozen action hash uses canonical JSON plus SHA-256.
- Approval creates a one-time grant bound to frozen action hash and target scope.
- Frontend provides an idempotency key.
- Gateway binds idempotency to execution replay; it does not authorize a new action.
- Approved actions execute automatically through Gateway; no second Execute click.
- Execution order is preflight -> mutation -> post-check.
- Preflight failure consumes the grant and blocks mutation.
- Post-check failure marks rollback required.
- Target locks prevent conflicting mutations.

## Contracts

- Supported input action types:
  - `restart_deployment`
  - `scale_deployment`
  - `rollback_deployment`
- Required target fields: `cluster`, `namespace`, `service`, `team`, and
  `deployment`. `scale_deployment` also requires `replicas`.
- Frozen action hash is SHA-256 over canonical JSON of action type, target,
  params, preflight, mutation, post-check, rollback, evidence refs, requester,
  and incident/run refs.
- Grant records are single-use and bound to action hash, scope, approval id or
  policy hit id, and actor.
- Locks are keyed by `kubernetes:{cluster}:{namespace}:deployment/{deployment}`.
- Idempotency key replay returns the original action/execution instead of
  authorizing another mutation.
- Self-approval remains governed by policy. First pass keeps the existing
  approver permission check and records whether approver equals requester.

## Deferred

- Host backend.
- Custom webhooks.
- Multi-approver approval chains.
- Broad low-risk auto-execution beyond policy-granted paths.
