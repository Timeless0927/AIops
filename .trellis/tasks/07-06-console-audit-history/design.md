# Design

## Boundary

Read-only frontend plus optional tiny Gateway audit read route. No new audit
store.

## Data Flow

Use diagnosis-process timeline first. If a recent audit API is needed, wrap the
existing `toolsets.audit_log` read path instead of duplicating storage.

## UI Shape

- Timeline list grouped by incident.
- Approval audit refs embedded in approval detail.
- Recent Gateway audit table if API exists.

## Compatibility

Existing audit rows remain unchanged.
