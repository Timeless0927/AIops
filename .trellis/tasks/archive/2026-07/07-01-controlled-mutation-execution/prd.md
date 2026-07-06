# Controlled mutation execution guardrails

## Goal

Enable an opt-in, controlled mutation execution path only after approval, audit,
dry-run, operation lock, post-check, and rollback guardrails are in place.

## Confirmed Facts

- Default profiles are read-only.
- `deploy/k8s/overlays/dev-remediation-rbac` exists as an opt-in RBAC extension.
- Current product boundary says P0/P1 diagnosis can propose actions but must not
  execute mutation automatically.

## Requirements

1. Mutation execution must require an approved internal Gateway approval request.
2. The default deployment profile must remain read-only.
3. Execution must support dry-run or preflight validation before mutation.
4. Operation lock/idempotency must prevent duplicate execution of the same grant.
5. Gateway audit and incident timeline must record request, decision, execution,
   post-check, and rollback-required states.
6. Failed post-check must produce a rollback-required notification without
   silently retrying mutation.

## Acceptance Criteria

- Tests prove unapproved, expired, rejected, duplicate, and out-of-scope mutation
  requests fail closed.
- Opt-in remediation RBAC can execute one controlled test mutation in a safe
  namespace after approval.
- Audit/timeline records are durable and queryable.
- Documentation clearly separates read-only default validation from mutation
  test setup.

## Out of Scope

- Autonomous remediation without human approval.
- Broad cluster-admin permissions.
- Feishu-owned approval state.
