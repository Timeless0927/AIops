# Design

## Boundary

Frontend-first over existing Gateway Notification Center APIs. Add only a small
read endpoint if current delivery payload is not exposed.

## Data Flow

`React -> /api/notifications/types -> Gateway`

`React -> /api/notifications/deliveries -> Gateway`

## UI Shape

- Summary counters: succeeded, failed, pending/dead-letter.
- Delivery table.
- Detail drawer/panel for error payload.

## Compatibility

Legacy migration happens in `notification-center-legacy-migration`; this task
only displays Gateway-owned state.
