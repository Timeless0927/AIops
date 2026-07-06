# Notification center legacy migration

## Goal

Migrate legacy Feishu notification paths to Gateway Notification Center; keep Feishu notification-only, with Gateway owning approval state.

## Requirements

- Migrate remaining legacy Feishu notification senders into Gateway Notification Center paths.
- Feishu may remain as a notification delivery channel only.
- Gateway Notification Center must own notification and approval state.
- Do not build Console UI for this task, except test-supporting API or documentation changes if required.
- Preserve existing behavior for diagnosis approval flows while removing direct legacy sender ownership.

## Acceptance Criteria

- [x] Grep confirms remaining `publish_approval_card`, legacy Feishu sender, and notification references were reviewed in `apps`, `hermes`, `hooks`, `runtime`, `toolsets`, `tests`, and `docs`.
- [x] Legacy Feishu notification code no longer owns approval state.
- [x] Gateway Notification Center is the state boundary for migrated notification behavior.
- [x] Relevant minimal tests pass.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
