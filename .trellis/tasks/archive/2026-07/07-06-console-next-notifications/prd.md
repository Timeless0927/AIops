# Console Next notification center delivery records

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver Console notification center behavior and external notification delivery
records for the next Console. Notifications remain Gateway-owned. Feishu and
external channels are notification-only and never own approval state.

The completed slice should be demoable as: approval becomes pending, Console
notification appears, Feishu/external delivery is recorded with retry/dead
letter behavior, approval state can only change through internal Gateway API,
and delivery record links into audit.

## User Stories Covered

- Parent stories 32, 49, 50, and 55.

## Requirements

- Notifications include Console notification center, Feishu/external channels,
  and page-level realtime toast/SSE events.
- Triggers include new incident, agent waiting for human input, approval
  pending, approval approved/rejected, execution succeeded/failed or
  rollback-required, settings/users permission changes, and no approver blocked
  state.
- Console notifications are scope-filtered.
- Feishu mapping is service/team to channel.
- First version does not need complex personal notification preferences.
- Notification delivery records link into audit and responsibility chains.
- Feishu/external notifications are notification-only and cannot approve,
  reject, or mutate approval state.
- Notification failure does not block Console approval.
- Delivery records include status, attempts, dedupe, retry, dead-letter, target
  message id where available, and sanitized error.
- Notification settings integrate with settings/policy slice.

## Acceptance Criteria

- [ ] Console notification center lists scoped notifications.
- [ ] Page-level realtime notification events appear for relevant active pages.
- [ ] Feishu/external delivery records are created for configured triggers.
- [ ] Delivery failure records retry/dead-letter state without blocking approval.
- [ ] Feishu/external payloads contain links only, no approval mutation actions.
- [ ] Delivery records link to audit responsibility chains.
- [ ] Notification configuration respects service/team mapping.
- [ ] Tests cover scope filtering, trigger creation, delivery records, dedupe,
  retry/dead-letter, failure isolation, and notification-only Feishu behavior.

## Blocked by

- Console Next settings versions and policy management.
- Console Next action grants, approvals, execution, and locks.
- Console Next responsibility-chain audit and tombstones.

## Further Notes

Existing notification center behavior should be reused where it fits. Do not
revive Feishu-native approval as an authoritative path.
