# Design

## Boundary

Frontend-only unless an existing Gateway response is missing a field the UI
already needs. Browser requests remain relative to Console Web and are proxied to
Gateway by Nginx.

## Data Flow

`React -> /api/incidents/active -> Gateway`

`React -> /api/incidents/{incident_id}/diagnosis-process -> Gateway`

## UI Shape

- Left/primary list: active incidents.
- Right/detail panel: incident metadata and diagnosis process.
- Secondary sections: evidence, timeline, missing evidence, action proposals.

## Compatibility

Keep current login/session behavior and existing static fallback data. Do not
change K8S manifests unless frontend routing requires it.
