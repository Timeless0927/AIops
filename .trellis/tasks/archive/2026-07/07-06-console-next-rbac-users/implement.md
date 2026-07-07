# Console Next RBAC/users implementation plan

## Order

1. Update identity domain model
   - Add target roles: `viewer`, `operator`, `approver`, `auditor`, `admin`.
   - Keep explicit legacy mapping:
     - `admin` -> `admin`
     - `user` -> `viewer`
     - `oncall_approver` -> `operator + approver`
   - Extend `Scope` to include `clusters`, alongside `namespaces`, `services`,
     and `teams`.
   - Preserve fail-closed behavior for missing resource scope on non-admin users.
   - Migrate existing stored users without cluster scope to `clusters=["*"]`.
   - Keep old config spellings (`services`, `teams`, `namespaces`) compatible.

2. Add minimal user-management domain operations
   - Extend `SQLiteIdentityStore` with list/create/update/disable/reset-password
     operations instead of adding a new store.
   - Store user source (`local`/`ldap`) and disabled state; users are disabled,
     not deleted.
   - Block disabling the last admin.
   - Block removing the current actor's own final admin capability.
   - Reject local user creation/update when any of cluster/namespace/service/team
     scope is missing.
   - Do not allow password reset for LDAP users.

3. Add Gateway permissions and routes
   - Add only the permissions needed by this slice, likely:
     - `view_users`
     - `manage_users`
     - `view_policy`
     - `view_settings`
   - Wire `/auth/me` to return capabilities from the new matrix.
   - Add Gateway-only `/api/users` routes:
     - `GET /api/users`
     - `POST /api/users`
     - `PATCH /api/users/{user_id}`
     - `POST /api/users/{user_id}/disable`
     - `POST /api/users/{user_id}/reset-password`
   - Authorize reads with auditor/admin visibility and writes with admin/manage
     permission.
   - Reuse `_authorize`, `_request_session`, and CSRF behavior from the routing
     slice. Cookie-authenticated writes must require `X-CSRF-Token`; bearer tests
     remain compatible.
   - Record audit rows for every user and permission change.

4. Add `/users` Console page behavior
   - Replace the placeholder `/users` page with a minimal management view.
   - Fetch only Gateway `/api/users`.
   - Show source, roles, four-dimensional scopes, disabled state, recent login,
     and recent permission-change audit.
   - Show admin-only create/disable/reset/role-scope controls.
   - Hide password reset controls for LDAP users.
   - Route guards and navigation should use capabilities from `/auth/me`, not
     hard-coded legacy role names.
   - Keep labels Chinese-first with English translations.

5. Update seed/config and compatibility tests
   - Update identity config fixtures and deployment config to use new role names
     where appropriate.
   - Keep legacy role inputs in tests to prove mapping compatibility.
   - Keep service-token and existing Gateway auth tests green.

## Validation commands

- `rtk npm run build` from `apps/aiops_console_web`
- `rtk test pytest -q tests/test_gateway_identity_rbac.py`
- Add and run a focused Gateway user-management test file, likely
  `rtk test pytest -q tests/test_gateway_users.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- Re-run auth-adjacent regression coverage:
  - `rtk test pytest -q tests/test_gateway_console_next_session.py`
  - `rtk test pytest -q tests/test_gateway_approval_service.py`
  - `rtk test pytest -q tests/test_command_gateway_skeleton.py`
- `rtk python3 -m py_compile aiops/domain/identity.py apps/aiops_k8s_gateway/main.py`

## Review gates

- No new auth boundary outside Gateway.
- No frontend calls outside same-origin Gateway paths.
- No new database/store abstraction unless `SQLiteIdentityStore` cannot cover it.
- Legacy role names remain accepted and tested.
- New users require explicit `cluster/namespace/service/team` scope.
- Existing upgraded users get `cluster=["*"]` only as migration compatibility,
  not as the default for newly created users.
- Last-admin and self-final-admin protections are server-side checks, not just UI
  disabled buttons.
- Auditor is read-only.
- Admin user management does not create an action-policy bypass; mutation policy
  remains owned by later policy/action slices.

## Rollback points

- Role/scope migration is additive: new constants, aliases, cluster scope column
  rows, and compatibility mapping. Roll back by restoring the old matrix and
  ignoring `cluster` scope rows.
- `/api/users` is additive. If UI work breaks, keep the backend route tests and
  revert only the frontend page.
- Console navigation can fall back to route guards from the routing slice if
  capability-driven hiding needs adjustment.

## Out of scope

- OAuth/OIDC.
- Tenant/org-level roles.
- User language preference API.
- Policy/action execution semantics beyond the capability checks needed for
  pages and APIs in this slice.
- Full audit-chain page behavior; this slice only surfaces recent permission
  change audit data needed by `/users`.
