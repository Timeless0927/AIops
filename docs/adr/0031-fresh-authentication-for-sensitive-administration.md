# Sensitive administration requires fresh authentication

Status: accepted

Administrative writes that affect identity, roles, Approval Authority, passwords, Connector Enrollments, Resource Bindings, Cluster Environment, or mutation enablement require the actor's current password to have been reverified within five minutes; V1 does not require a second administrator. Disabling a User or changing that User's password, roles, scope, or authority immediately revokes all of their sessions, and authorization reads current state rather than a login-time snapshot. Every attempted change records actor, target, reason, structured before and after values, result, and request ID without secrets, while the final active Platform Administrator remains protected from removal or disablement.
