# Connector identities are pre-provisioned per cluster

Status: accepted

Each Connector uses its own pre-provisioned credential bound to one `connector_id` and one immutable `cluster_id`. Gateway stores only the credential hash and accepts registration, heartbeat, command polling, and result submission only when the authenticated identity matches both IDs. Enrollment does not create a visible Cluster; the first successful authenticated registration does. Credentials are independently revocable and rotatable, and AIOps rejects shared Connector secrets and trust-on-first-use registration.
