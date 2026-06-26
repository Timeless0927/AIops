# Directory Structure

> Where backend code lives. Boundaries match project `CLAUDE.md` and are enforced
> by `tests/test_architecture_boundaries.py`.

---

## Top-level layout

```
apps/              # runnable services (one process per subdir)
  aiops_k8s_gateway/   # control-plane Gateway: RBAC, approval, audit, routing
  cluster_connector/   # in-cluster kubectl executor (read-only by default)
  aiops_console/       # static frontend vertical slice (see frontend specs)
  mcp_prometheus/      # single-tool observability MCP HTTP facade
  mcp_loki/            # single-tool observability MCP HTTP facade
  mcp_topology/        # single-tool observability MCP HTTP facade
  service_http.py      # shared JsonHandler / serve() / connectivity_payload
  observability_http.py# make_handler() factory for MCP facades
aiops/
  contracts/           # shared V1 envelopes: envelope.py, errors.py, evidence.py, writeback_auth.py, time_range.py
  domain/              # domain models: identity.py (Actor/Scope/RBAC), incident.py, grant.py, command_task.py, topology.py, service_identity.py
  k8s/                 # command_envelope.py, result_envelope.py
hermes/                # diagnosis service: service_main.py + diagnosis orchestration
toolsets/              # durable stores + legacy tool registry: audit_log.py, incident_store.py, ...
runtime/               # V1 legacy compatibility (workers, smoke) — new logic does NOT land here
hooks/                 # V1 legacy compatibility layer — new logic does NOT land here
tests/                 # pytest suite, flat layout
```

---

## Rules backed by code

- **One process per `apps/<service>/`**, each with its own `main()` calling
  `apps.service_http.serve(Handler, host=, port=)`. See `apps/aiops_k8s_gateway/main.py:1007`,
  `hermes/service_main.py:659`.
- **Cross-service contracts live in `aiops/contracts/`**, not in a service's own
  package. Gateway <-> Connector speaks `aiops/k8s/command_envelope.py`; MCP
  facades return `aiops/contracts/envelope.ToolEnvelope` (`apps/observability_http.py:25`).
- **No service imports another service's internals.** Gateway talks to Connector
  by HTTP route registration (`/connectors/register`) and to MCP facades by URL
  env vars (`AIOPS_PROMETHEUS_MCP_URL`, etc., in `hermes/service_main.py:363`).
- **Domain logic lives in `aiops/domain/`.** RBAC (`identity.py`), incident status
  machine (`incident_store.py:79 _ALLOWED_TRANSITIONS`), grants are dataclasses.
- **`toolsets/` holds durable stores + legacy Hermes-tool registry.** New Gateway
  stores (e.g. approval) live as a service module (`apps/aiops_k8s_gateway/approval_service.py`),
  not in `toolsets/`, unless they are shared by Hermes tools too.
- **`hooks/` and `runtime/` are legacy.** Don't add new domain logic there;
  `tests/test_approval_authorization.py` still loads `hooks/approval_authorization.py`
  by path, but new authorization is `aiops/domain/identity.py` `Actor.can`.

---

## Anti-patterns

- Putting HTTP route logic in `aiops/` or `toolsets/` — those are contract/domain
  layers; routes belong in `apps/<service>/`.
- A service importing `apps/<other_service>/*.py` internals directly. Use the HTTP
  contract or an `aiops/contracts/` envelope.
- Adding a new durable store to `toolsets/` that only the Gateway needs — keep it
  in `apps/aiops_k8s_gateway/` instead.
