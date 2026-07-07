# Console Next five-role RBAC and user management

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver the target Console identity and authorization model: five platform
roles, four-dimensional scopes, local and LDAP users, and `/users` management.
This slice must migrate or map existing role concepts into the new model without
breaking current Gateway authorization behavior.

The completed slice should be demoable as: admin logs in, creates a local
operator, assigns cluster/namespace/service/team scope, and that user can view
scoped incidents but cannot access admin-only pages. Every permission change is
visible in audit.

## User Stories Covered

- Parent stories 3, 7, 8, 9, 10, 11, 12, 13, 20, 28, and 54.

## Requirements

- Target roles are `viewer`, `operator`, `approver`, `auditor`, and `admin`.
- Existing role names are migrated or compatibility-mapped explicitly.
- Authorization scope uses `cluster`, `namespace`, `service`, and `team`.
- Non-admin users are scope-limited.
- Missing resource scope fails closed for non-admin users.
- Admin can manage users and scopes globally, but admin does not bypass action
  policy.
- Auditor has read-only audit, policy, user, and configuration visibility.
- Local users can be created, disabled, reset, and assigned roles/scopes.
- LDAP users can log in when LDAP is configured and receive local role/scope
  mappings.
- Console cannot change LDAP passwords.
- Users are disabled, not deleted.
- Last admin cannot be disabled.
- A user cannot remove their own final admin capability.
- Recent login and recent permission-change audit are visible.
- Permission-filtered navigation and route guards use Gateway capabilities.

## Acceptance Criteria

- [ ] Five target roles exist with documented permissions.
- [ ] Legacy roles have a tested migration or compatibility mapping.
- [ ] Four-dimensional scope checks are enforced by Gateway.
- [ ] Missing cluster, namespace, service, or team scope fails closed for
  non-admin users.
- [ ] `/users` lists users with source, roles, scopes, disabled state, recent
  login, and recent permission-change audit.
- [ ] Admin can create and disable local users, reset local passwords, and
  assign roles/scopes.
- [ ] LDAP users can authenticate when configured, but password reset controls
  are unavailable for LDAP users.
- [ ] Last-admin and self-final-admin protections are enforced server-side.
- [ ] User and permission changes are audited.
- [ ] Unauthorized write attempts return correct API errors and render correctly
  in the Console.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.

## Further Notes

This slice should not implement policy/action behavior beyond the role and
scope capabilities needed to gate pages and APIs.
