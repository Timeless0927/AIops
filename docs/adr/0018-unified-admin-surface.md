# Console has one restricted administration surface

Status: accepted

Console adds one permission-gated `/admin` surface for Platform Administrators, superseding the earlier four-route rule that excluded all administration. It manages Users, Teams, memberships, Role Bindings, Approval Authority, Connector Enrollments, Discovery Candidates, Resource Bindings, Cluster Environment, and mutation enablement through tabs rather than separate product areas. Ordinary SRE navigation remains Incident-only, and administration is entered from the user menu rather than the primary workflow. CLI access is limited to bootstrap and recovery.
