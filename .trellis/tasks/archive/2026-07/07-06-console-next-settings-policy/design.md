# Console Next settings/policy design

## Scope

This slice delivers versioned settings and policy visibility/editing through
Gateway.

## Confirmed decisions

- `/policies` starts as a read-heavy explanation surface.
- `/settings` owns writes for policy/settings changes.
- Unconfigured clusters are treated as `prod`.
- Default policy:
  - `prod`: all mutations require approval.
  - `staging`: low-risk mutations may receive policy grants; medium/high require approval.
  - `dev` / `test`: low-risk automatic execution is configurable.
- Settings saves are immediate and versioned.
- Save flow shows diff first.
- Critical changes require confirmation text.
- Secrets, tokens, LDAP bind passwords, internal service URLs, and database paths
  are not editable or displayed.
- Rollback creates a new audited version rather than deleting history.

## Deferred

- Draft/publish settings workflow.
- Full policy simulation UI beyond a first `Test policy` affordance.
- Multi-replica config distribution.
