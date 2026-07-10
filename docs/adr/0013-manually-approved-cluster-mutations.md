# V1 mutations are opt-in and manually approved

Status: accepted

Every Cluster is read-only by default. An administrator may explicitly enable mutation for a Cluster, after which an Agent may produce evidence-grounded Recommended Actions but can never approve or execute them. Gateway may issue an Execution Grant only for a frozen allowlisted action that has eligible human approval, complete resource binding, sufficient evidence, and available rollback; Connector independently validates the grant, Cluster, namespace, action, and parameters before execution. Preflight, an execution lock, post-check, and rollback handling are mandatory. Automatic approval and automatic execution are outside V1.
