# V1 uses metrics and structured logs before tracing

Status: accepted

Every AIOps service exposes actionable Prometheus metrics and emits structured JSON logs to stdout. Metrics cover bounded-label HTTP RED signals and each service's durable work: queue depth and oldest age, Connector heartbeat age, Diagnosis outcomes and duration, Connector Command leases and Unknown Outcomes, Notification Delivery retries and dead letters, SSE connections, and SQLite failures. A small PrometheusRule set alerts on control-plane unavailability, stalled durable work, Unknown Outcomes, dead letters, and storage pressure.

Logs propagate `request_id` and `correlation_id` across Gateway, Diagnosis, Connector, and Notification Engine boundaries and include only relevant domain identifiers. Metrics never label by unbounded Incident, User, Command, or Delivery identity, and logs exclude credentials, session material, raw evidence, and full request bodies. V1 does not deploy OpenTelemetry SDKs, a Collector, or a tracing backend; distributed tracing is reconsidered only if correlated metrics and logs prove insufficient.
