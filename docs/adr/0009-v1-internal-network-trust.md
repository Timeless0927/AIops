# V1 trusts a restricted cluster network for internal transport

Status: accepted

Console, Alertmanager, and cross-cluster Connector traffic to Gateway must use HTTPS. Calls among Gateway, Diagnosis, and MCP processes may use ClusterIP HTTP in V1, protected by short-lived audience-scoped ServiceAccount identities and strict NetworkPolicy; internal routes must not be exposed through public Ingress or NodePort. This deliberately accepts the risk that a node-level or cluster-network observer could read internal traffic during the internal trial. Automated internal TLS or mTLS is a production-GA requirement, not a V1 dependency.
