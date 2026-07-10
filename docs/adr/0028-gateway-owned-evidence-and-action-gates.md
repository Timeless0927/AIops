# Gateway owns Evidence Steps and action gates

Status: accepted

Gateway owns the canonical, durable Evidence Steps for each Investigation; Diagnosis submits idempotent step facts, while MCP services and Connectors return observations without owning product state. Evidence Steps use `running`, `succeeded`, `partial`, `failed`, or `skipped`, and Gateway evaluates their resource scope, freshness, reference integrity, and action-specific requirements through a deterministic Evidence Gate. An incomplete gate may still yield a visible judgment and missing-evidence guidance, but cannot yield an approvable mutation; AI confidence and Human Input cannot bypass the gate.
