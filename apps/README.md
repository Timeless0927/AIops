# Application Boundaries

`apps/` contains runnable process boundaries and the independently built Console workspace. Python adapters remain thin around shared `aiops/*` contracts and domain code.

V1 process boundaries:

- `aiops_k8s_gateway`: K8s Gateway MCP facade, command task orchestration, policy, approval, audit, and Connector routing.
- `cluster_connector`: in-cluster executor that connects outbound to Gateway and executes approved command envelopes.
- `mcp_prometheus`: Prometheus MCP facade and routing.
- `mcp_loki`: Loki MCP facade and routing.
- `mcp_topology`: Topology MCP facade and service dependency queries.
- `aiops_console_web`: React Console source workspace with its own package lock and build; it consumes Gateway's versioned OpenAPI contract and is not bundled into the Gateway image.

Do not put shared business rules here. Shared rules belong in `aiops/domain`, `aiops/contracts`, `aiops/policy`, `aiops/approval`, `aiops/audit`, or `aiops/k8s`.
