# Notifications run in an independent multi-channel service

Status: accepted

Notification Engine is a separate Kubernetes Deployment rather than a Gateway module. Gateway persists a channel-neutral Notification Request and retries authenticated handoff until Notification Engine durably accepts it with `202`; Notification Engine then owns routing, channel rendering, delivery state, and provider retries without gaining authority over Incident, Approval, or execution state. Browser administration remains behind Gateway, service failure never blocks the originating business transition, and V1 uses a single-replica SQLite volume without a message broker.
