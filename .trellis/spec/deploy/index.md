# Deploy Specs

> Deploy-time contracts for the split AIOps services on Kubernetes. Source-backed
> against `deploy/k8s/` and the runtime env consumed by `apps/` / `hermes/` /
> `toolsets/`.

## Guides Index

| Guide | What it covers |
|-------|----------------|
| [dev-external Observability Backend Contract](./dev-external-observability-contract.md) | env wiring (`PROMETHEUS_URL` / `LOKI_URL` / `AIOPS_NAMESPACE_SCOPE`), backend reachability + label contract, AIOps-app stdout-logging prerequisite, AIOps-own ServiceMonitor prerequisite, and the diagnosis empty-backend tolerance gate that the ADR-0005 Issue A end-to-end smoke asserts against |

**Scope note**: these specs cover the deploy/overlay layer and the runtime env
handoff into the services. They do *not* duplicate `docs/adr/0005` (motivation)
or `deploy/k8s/README.md` (command reference); they capture the executable
contract that makes a `dev-external` smoke pass or fail observably.