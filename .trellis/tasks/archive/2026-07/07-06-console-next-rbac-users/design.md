# Console Next RBAC/users design

## Scope

This slice migrates the current identity/RBAC shape to the Console Next role and
scope model, and delivers `/users` management.

## Confirmed decisions

- Target roles are `viewer`, `operator`, `approver`, `auditor`, and `admin`.
- Legacy role mapping:
  - `admin` -> `admin`
  - `user` -> `viewer`
  - `oncall_approver` -> `operator + approver`
- Existing users without cluster scope migrate to `cluster=["*"]` so an upgrade
  does not silently remove all access.
- New users must explicitly configure `cluster`, `namespace`, `service`, and
  `team` scope.
- Missing resource scope still fails closed for non-admin users after migration.
- Admin can manage users and scopes, but admin does not bypass action policy.
- LDAP users can authenticate and receive local role/scope mappings, but Console
  cannot reset LDAP passwords.
- Users are disabled, not deleted.
- Last admin and self-final-admin removal are blocked server-side.

## Deferred

- OAuth/OIDC.
- Tenant/org-level roles.
- Rich user preference APIs, including language preference.
