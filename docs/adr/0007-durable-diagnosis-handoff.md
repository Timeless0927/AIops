# Diagnosis handoff is durable and retried

Status: accepted

Gateway persists a Diagnosis Request when it creates or reuses an Incident and retries delivery with backoff while Diagnosis is unavailable. Diagnosis accepts requests idempotently by `session_id`, persists a Diagnosis Job before returning `202 Accepted`, and then owns execution and writeback. Gateway marks the request accepted only after that response; until then the Incident remains visible as waiting for Diagnosis. This avoids losing diagnosis work during restarts without introducing a message broker in V1.
