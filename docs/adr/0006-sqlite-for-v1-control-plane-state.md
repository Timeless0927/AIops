# V1 keeps single-replica SQLite state

Status: accepted

AIOps V1 is an internal trial with one active replica of each control-plane process and SQLite state on the existing PVC. Diagnosis jobs and Connector commands must be durable across process restarts, while brief unavailability and renewed login or Connector sessions during a restart are acceptable. PostgreSQL and multi-replica control-plane availability are deferred until production GA or until measured concurrency, failover, or storage constraints require them; V1 will not add a speculative storage abstraction solely for that future migration.
