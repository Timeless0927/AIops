# Console Next responsibility-chain audit design

## Scope

This slice makes responsibility chains the default audit experience.

## Confirmed decisions

- `/audit` defaults to responsibility chains, not raw logs.
- Raw logs live in a secondary tab.
- Audit chain IDs are aggregated around action, execution, and approval
  responsibility records.
- A chain must answer:
  - what the agent requested
  - why it requested it
  - who approved it
  - what Gateway executed
  - what happened afterward
- Conversation deletion removes chat-layer content only.
- Conversation deletion keeps tombstone and responsibility refs.
- Immutable responsibility records cannot be deleted.
- Notification delivery refs can attach to chains.

## Deferred

- Full compliance export format beyond a basic export affordance.
- External SIEM integration.
