# ADR-0002: V1 repository boundaries for Gateway, Connector, MCP, and shared contracts

Date: 2026-06-04

Status: Accepted

## Context

AIO-47 through AIO-51 changed the product shape from a single Hermes agent with local tools into a platform made of Agent Center, multiple MCP facades, AIops K8s Gateway, Cluster Connector, topology registry, and incident learning.

AIO-61 fixed packaging and image smoke problems around the legacy `toolsets` package. That work is useful as a CI gate, but it does not define the long-term source layout. If new V1 work continues to land directly in `toolsets/` and `runtime/`, the codebase will keep the single-agent shape while the architecture has already moved to a platform shape.

## Decision

Create explicit repository boundaries:

- `apps/` contains runnable process entry points and delivery adapters.
- `apps/aiops_k8s_gateway/` contains the K8s Gateway process boundary.
- `apps/cluster_connector/` contains the in-cluster Connector process boundary.
- `apps/mcp_prometheus/`, `apps/mcp_loki/`, and `apps/mcp_topology/` contain MCP facade process boundaries.
- `aiops/contracts/` contains pure shared request/response envelopes, errors, evidence references, and time-range types.
- `aiops/domain/` contains pure domain models such as `CommandTask`, `Grant`, `ServiceIdentity`, topology edges, and incident records.
- `aiops/policy/`, `aiops/approval/`, `aiops/audit/`, and `aiops/k8s/` contain platform shared capabilities behind stable package boundaries.
- `toolsets/`, `hooks/`, and `runtime/` remain as legacy compatibility layers during V1. They can call into `aiops/*`, but new domain logic should not be added there by default.

This ADR intentionally creates a lightweight skeleton first. It does not move existing business logic in bulk.

## Dependency Rules

Allowed:

- `apps/*` may depend on `aiops/*`.
- `toolsets/*`, `hooks/*`, and `runtime/*` may call `aiops/*` during migration.
- `aiops/k8s` may depend on `aiops/contracts` and `aiops/domain`.
- `aiops/policy`, `aiops/approval`, and `aiops/audit` may depend on contracts and domain models.

Forbidden:

- `aiops/domain` must not import `toolsets`, `hooks`, `runtime`, Hermes registry modules, HTTP clients, or Kubernetes clients.
- `aiops/contracts` must not import application, runtime, toolset, or infrastructure modules.
- Gateway code must not import Connector internals directly. Gateway and Connector communicate through contracts and envelopes.
- MCP facades must not call Gateway internals for observability data. Prometheus, Loki, and Topology remain separate MCP boundaries.

## Migration Order

1. Keep AIO-61 packaging fixes and image smoke as the temporary integration gate.
2. Add this skeleton and architecture boundary tests.
3. Move shared query contract pieces, time-range parsing, errors, and evidence references into `aiops/contracts`.
4. Move reusable audit helpers into `aiops/audit`, leaving `toolsets/audit_log.py` as a compatibility wrapper.
5. Add K8s Gateway `CommandTask`, `Grant`, command envelope, and result envelope implementations under `aiops/domain` and `aiops/k8s`.
6. Add Gateway, Connector, and MCP process implementations under `apps/*`.
7. Split Dockerfile, entrypoint, and CI matrix only after the process boundaries have real code.

## Consequences

Positive:

- V1 work has clear landing zones.
- Domain and contract code can remain stable when Hermes, Codex, or another Brain Provider changes.
- Gateway and Connector stay decoupled by protocol rather than Python imports.
- Legacy imports continue to work during migration.

Costs:

- More packages exist before all of them contain full implementations.
- Contributors must follow dependency rules instead of placing all new code in `toolsets/`.
- CI needs architecture fitness tests to keep the boundaries from drifting.

## Alternatives Considered

Continue using `toolsets/` as the main implementation directory:

- Benefit: least short-term movement.
- Cost: preserves the single-agent structure and hides Gateway, Connector, MCP, and domain boundaries.

Move all existing code immediately:

- Benefit: cleaner final layout.
- Cost: high regression risk, large import churn, and unnecessary delay before V1 module work can continue.

Split into multiple repositories:

- Benefit: strong service ownership boundaries.
- Cost: premature for the current team and V1 stage; shared contract churn would become harder to coordinate.
