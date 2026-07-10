# Diagnosis retries each failure boundary independently

Status: accepted

Gateway retries transient Diagnosis Request delivery with exponential backoff and jitter until its acceptance deadline; contract or identity errors reject it immediately, while an unaccepted deadline expires it and fails the Investigation. Diagnosis durably leases an accepted Diagnosis Job and uses bounded retries only for whole-job crashes or timeouts; individual evidence-source failures produce `partial` or `needs_human` outcomes instead of rerunning the Job. Diagnosis persists completed artifacts and retries idempotent writeback independently until Gateway acknowledges them, so writeback failure never repeats diagnosis work. A terminally failed or expired Investigation requires explicit human retry, and repeated Alert Signals do not create concurrent Diagnosis Jobs.
