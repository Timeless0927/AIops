# V1 retains governance records and expires technical churn

Status: accepted

V1 does not automatically delete Incidents, Investigations, Investigation Events, Evidence references and retained summaries, Approvals, Connector Commands and execution outcomes, audit records, identity and policy changes, or published Incident Reports. These records are the durable governance history. Raw Prometheus samples and Loki logs are not copied into the service databases and follow their owning observability backend's retention policy.

High-churn operational data has fixed V1 bounds. Connector heartbeat writes replace current state and only state transitions become durable history. A Connector may delete acknowledged terminal journal data after 30 days, and Diagnosis may delete terminal internal Job and scratch data after 30 days once its retained result and Evidence references are safely owned by Gateway. Notification Engine deletes terminal Notification Requests, Deliveries, and rendered content after 90 days. Expired sessions, leases, and locks are removed promptly. Unresolved dead letters and Unknown Outcomes are never expired automatically. V1 adds bounded periodic cleanup and storage-pressure metrics, but no archive service or configurable retention matrix until measured volume or policy requires one.
