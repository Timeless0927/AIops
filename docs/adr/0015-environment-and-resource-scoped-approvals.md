# Approval authority intersects environment and resource bindings

Status: accepted

Approval Authority is granted through an explicit binding whose Environment and resource scope must both cover the frozen action target. Resource scope resolves through Team ownership of Services and Service deployment to Deployment Targets; unresolved resources cannot receive an Execution Grant. Platform-wide or environment-wide authority is possible only through an explicit wildcard binding and is never implied by `platform_admin`. Bindings reference registered domain identities rather than free-text cluster, namespace, service, or team lists.
