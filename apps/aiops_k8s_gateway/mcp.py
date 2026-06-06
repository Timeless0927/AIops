"""Gateway MCP facade boundary.

The V1 external tools remain:

- run_k8s_read
- submit_k8s_change
- get_k8s_execution_status

Implementation should depend on `aiops.contracts`, `aiops.domain`, `aiops.policy`,
`aiops.approval`, `aiops.audit`, and `aiops.k8s`, not on Connector internals.
"""

