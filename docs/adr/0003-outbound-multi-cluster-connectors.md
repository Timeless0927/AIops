# Central Gateway with outbound multi-cluster Connectors

Status: accepted

AIOps V1 serves one organization and multiple Kubernetes clusters. Each cluster runs one Connector configured with the central Gateway address and its own credential; the Connector initiates the authenticated connection and is bound to exactly one `cluster_id`. Gateway does not require inbound network access to managed clusters, and Console configuration cannot create cluster identity or runtime state. This replaces the current fixed `AIOPS_CONNECTOR_URL` direct-call assumption; the exact outbound transport will be decided separately.
