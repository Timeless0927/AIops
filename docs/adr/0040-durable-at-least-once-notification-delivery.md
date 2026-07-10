# Notification delivery is durable and at least once

Status: accepted

Notification Engine persists every Notification Delivery and workers claim due work with leases. Network errors, timeouts, `429`, and `5xx` responses receive bounded exponential-backoff retries that honor `Retry-After`; non-retryable `4xx` responses and exhausted retries move the Delivery to dead-letter. After correcting its destination or credentials, an administrator may manually redeliver it.

A uniqueness constraint on event ID and destination prevents duplicate Delivery records. It cannot prevent duplicate external messages when a Provider accepted a request but its response was lost, because the V1 Providers do not share an idempotency contract. A Delivery may therefore make more than one transport attempt: AIOps prefers a rare duplicate over silently losing a notification.
