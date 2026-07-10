# V1 correlates Incidents by workload and alert identity

Status: accepted

V1 correlates an active Incident by registered Cluster, namespace, Deployment Target or workload identity, and `alertname`. Individual Alertmanager fingerprints are retained as Alert Signals within that Incident, so many Pod alerts for one workload are grouped while identical alert names on different workloads remain separate. Missing workload identity falls back to a confirmed Deployment Target and then Service; an unresolved resource uses its fingerprint to create an isolated unbound Incident. Repeated signals append to the active Incident, and a resolved Incident reopens only within a configurable reopen window. Cross-`alertname` correlation is deferred.
