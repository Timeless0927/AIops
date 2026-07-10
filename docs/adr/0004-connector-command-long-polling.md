# Connector commands use authenticated HTTPS long polling

Status: accepted

Connector registers and sends heartbeats to Gateway, long-polls for commands assigned to its identity, then posts command results. Gateway persists every command before delivery and owns its lease, timeout, retry, and idempotency state; Gateway never initiates a connection into a managed cluster. HTTPS long polling was chosen over WebSocket and gRPC because it works through NAT and enterprise proxies, has straightforward reconnect semantics, and meets AIOps latency needs without adding another transport stack.
