# Application Boundaries

`apps/` contains runnable process boundaries. These packages are thin adapters around shared `aiops/*` contracts and domain code.

V1 process boundaries:

- `aiops_k8s_gateway`: K8s Gateway MCP facade, command task orchestration, policy, approval, audit, and Connector routing.
- `cluster_connector`: in-cluster executor that connects outbound to Gateway and executes approved command envelopes.
- `mcp_prometheus`: Prometheus MCP facade and routing.
- `mcp_loki`: Loki MCP facade and routing.
- `mcp_topology`: Topology MCP facade and service dependency queries.

Do not put shared business rules here. Shared rules belong in `aiops/domain`, `aiops/contracts`, `aiops/policy`, `aiops/approval`, `aiops/audit`, or `aiops/k8s`.
