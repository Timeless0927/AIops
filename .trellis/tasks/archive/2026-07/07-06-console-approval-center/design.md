# Design

## Boundary

Frontend plus minimal Gateway read/decision routes only if missing. Do not add an
execution engine here.

## Data Flow

`React -> /api/approval-requests -> Gateway`

`React -> /api/approval-requests/{approval_id} -> Gateway`

Decision endpoints should reuse existing Gateway approval service functions if
they exist; do not create a parallel approval store.

## UI Shape

- Approval list tab or page.
- Detail panel for selected approval.
- Decision buttons hidden/disabled unless request is pending and actor is
  authorized.

## Compatibility

Existing approval rows and audit refs stay unchanged.
