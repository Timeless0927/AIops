# Connector Command delivery is lease-based and mutation-conservative

Status: accepted

Gateway atomically grants a Command Lease, and Connector must report the start under that lease and receive Gateway acknowledgement before executing. An expired lease that never started may requeue the same command; a started read may use bounded automatic retries, but a started mutation is never automatically repeated and becomes Unknown Outcome when no trustworthy result arrives. Identical result submissions are idempotently accepted, conflicting submissions are rejected and audited, and late results are retained for reconciliation.
