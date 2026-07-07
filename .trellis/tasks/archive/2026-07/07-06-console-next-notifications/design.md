# Console Next notifications design

## Scope

This slice delivers Console notification center behavior and external delivery
records.

## Confirmed decisions

- Console notification center is the primary notification view.
- Feishu and external channels are notification-only.
- Feishu/external messages contain links only and cannot approve/reject.
- Delivery failure does not block approval or execution.
- Delivery records include status, attempts, dedupe, retry/dead-letter, target
  message id when available, and sanitized error.
- Dead-letter is visible and manually retryable in the first pass.
- Do not build complex automatic recovery strategies in this slice.
- Delivery records link into audit responsibility chains.

## Deferred

- Personal notification preferences.
- Feishu-native approval.
- Complex auto-remediation of failed delivery.
