# Console Next OpenObserve evidence design

## Scope

This slice makes evidence a first-class Gateway-owned model, with OpenObserve as
the preferred observability backend and current MCP/K8s paths as fallback.

## Confirmed decisions

- OpenObserve is the new primary path for logs, metrics, and traces.
- If OpenObserve is not configured or fails, Gateway degrades to existing
  Prometheus, Loki, Topology, and Kubernetes evidence where available.
- Ordinary users use templated evidence queries.
- Free-form/advanced queries are limited to `admin` and `auditor`, still
  scope-limited and audited.
- Agents use allowlisted tool templates only.
- Gateway always applies scope checks, row/time/timeout limits, and redaction.
- Missing scope fields fail closed for non-admin users.
- Raw evidence stays outside SQLite; store summaries and refs.

## Deferred

- Host evidence.
- User-authored arbitrary query builder for ordinary users.
- Long-term raw evidence retention inside Gateway storage.
