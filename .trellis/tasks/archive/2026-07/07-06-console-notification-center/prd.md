# Console notification center

## Goal

Expose Gateway Notification Center state in Console: notification types,
delivery records, failures, and dead-letter state.

## Position

- Order: 3
- Priority: P2
- Parallel: can run with audit history
- Depends on: notification-center-legacy-migration for complete production data
- Blocks: none

## Confirmed Facts

- Gateway already exposes notification type and delivery routes.
- Legacy Feishu direct send paths still need migration.

## Requirements

1. Show notification template/type catalog.
2. Show recent delivery records with type, target, status, attempts, and error.
3. Filter minimally by status/type if Gateway already supports it.
4. Do not let notification failures change approval state.
5. UI text must be Chinese.

## Acceptance Criteria

- [ ] Console can inspect recent notification deliveries.
- [ ] Delivery failures/dead-letter states are visible.
- [ ] No browser call goes outside Gateway.
- [ ] Tests cover Notification Center API paths.

## Out of Scope

- Replacing Feishu.
- Editing notification templates.
- Approval decisions.
