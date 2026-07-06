# Design

## Boundary

Frontend lifecycle display plus Gateway integration after
`controlled-mutation-execution` lands. No direct Connector/K8S calls from the
browser.

## Data Flow

`React -> Gateway execution/read endpoints -> Gateway-controlled execution store`

Until the backend task lands, show a disabled state sourced from approval/action
proposal metadata.

## UI Shape

- Execution status block inside approval/action detail.
- Lifecycle steps: preflight, execution, post-check, rollback-required.
- Execute button disabled unless Gateway says executable.

## Compatibility

Default deployments stay read-only.
