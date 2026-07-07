# Console Next responsibility-chain audit and tombstones

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver responsibility-chain audit as the default audit experience. The audit
view must answer what the agent requested, why, who approved, what Gateway
executed, and what happened afterwards. It also preserves immutable records and
conversation deletion tombstones.

The completed slice should be demoable as: auditor opens an audit chain for an
executed action and sees agent request, evidence refs, policy decision,
approval, frozen action/hash, execution result, notification refs, and delete
tombstones where relevant.

## User Stories Covered

- Parent stories 8, 13, 36, 39, 40, 50, and 55.

## Requirements

- `/audit` defaults to responsibility chains.
- Audit tabs include responsibility chains, raw logs, and deleted conversation
  records.
- Responsibility chain fields include time, incident, conversation/run, agent,
  requested action, risk, target resource, approver, approval decision, Gateway
  execution result, and responsibility status.
- Details include agent request, evidence refs, risk classification, frozen
  action/hash, approver identity snapshot, approval remark, preflight, mutation,
  post-check, notifications, delete tombstone, and raw audit refs.
- Immutable responsibility records cannot be deleted.
- Conversation deletion keeps tombstone with conversation id, deleted by,
  deleted at, reason, linked incident ids, linked approval ids, and linked
  execution ids.
- Audit data is permission-filtered.
- Raw logs remain available for users with audit permission.
- Audit records retain long-term accountability defaults from parent PRD.

## Acceptance Criteria

- [ ] `/audit` lists permission-filtered responsibility chains by default.
- [ ] `/audit/:chainId` shows full responsibility-chain details.
- [ ] Raw logs are linked but not the primary default view.
- [ ] Immutable records survive conversation deletion.
- [ ] Deleting chat-layer conversation content creates a tombstone.
- [ ] Notification delivery refs can appear in audit chains.
- [ ] Auditor read-only access works without production action privileges.
- [ ] Unauthorized users cannot infer hidden audit objects.
- [ ] Tests cover chain construction, permission filtering, immutable records,
  tombstones, notification refs, raw log refs, and hidden-resource behavior.

## Blocked by

- Console Next action grants, approvals, execution, and locks.
- Console Next incident workbench and run controls.

## Further Notes

Audit is about responsibility, not raw log volume. Prefer concise chain records
with refs over duplicating every raw event.
