# Console execution tracking

## Goal

Show controlled execution state in Console after approval: dry-run/preflight,
execution attempt, post-check, and rollback-required states.

## Position

- Order: 5
- Priority: P2
- Parallel: UI skeleton can start after approval center; real integration waits
  for controlled-mutation-execution
- Depends on: controlled-mutation-execution
- Blocks: none

## Confirmed Facts

- Default K8S profiles must remain read-only.
- Mutation execution must be opt-in and approval-gated.
- Console must not fake execution success without Gateway state.

## Requirements

1. Display execution eligibility from approved Gateway state.
2. Show dry-run/preflight, execution, post-check, and rollback-required statuses.
3. Disable execution controls unless Gateway exposes an approved execution grant.
4. Surface failure reason and rollback-required notification state.
5. UI text must be Chinese.

## Acceptance Criteria

- [ ] Before backend execution exists, Console shows read-only unavailable state.
- [ ] After controlled execution APIs exist, Console tracks real execution lifecycle.
- [ ] Duplicate/expired/unapproved grants are not executable from UI.
- [ ] Tests cover disabled and authorized states.

## Out of Scope

- Building mutation guardrails here.
- Autonomous remediation.
