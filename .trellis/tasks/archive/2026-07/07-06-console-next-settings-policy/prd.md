# Console Next settings versions and policy management

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver versioned Console settings and policy visibility. Admins can edit
non-secret operational settings, preview diffs, save immediate audited versions,
and roll back to the previous version. Operators and auditors can inspect policy
state and policy hits according to role.

The completed slice should be demoable as: admin changes approval policy for a
namespace, sees a diff, confirms a critical change, saves a version, sees the
audit record, sees the policy hit in `/policies`, then rolls back.

## User Stories Covered

- Parent stories 8, 13, 44, 45, 46, 47, and 48.

## Requirements

- `/settings` contains sections for cluster/environment, approval policy, action
  allowlist, notification configuration, security/session, and feature flags.
- Settings writes are admin-only.
- Auditor can view settings read-only.
- Save is immediate; no draft/publish workflow.
- Every save creates a configuration version.
- Frontend shows a diff before saving.
- Critical changes require confirmation text.
- All settings changes are audited.
- Rollback to the previous version is supported.
- Secrets, tokens, LDAP bind passwords, internal service URLs, and database
  paths are neither editable nor displayed.
- UI shows whether a saved setting needs restart or reload.
- `/policies` shows cluster/environment policy, action allowlists, and recent
  policy hits.
- Unconfigured clusters are treated as production.

## Acceptance Criteria

- [x] Admin can view, edit, diff, confirm, save, and roll back settings.
- [x] Auditor can view settings and policies but cannot write.
- [x] Settings saves create version records and audit records.
- [x] Rollback creates a new audited version based on the previous version.
- [x] Secret-like fields are absent from API responses and UI.
- [x] Critical changes require exact confirmation text.
- [x] `/policies` renders active policy, cluster environments, allowlists, and
  recent policy hits.
- [x] Unconfigured cluster policy is treated as production.
- [x] Settings and policy API errors render retryable or validation states.
- [x] Tests cover versioning, rollback, secret hiding, admin-only writes, and
  policy-hit visibility.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.
- Console Next five-role RBAC and user management.

## Further Notes

Do not build a generic environment-variable editor. Only product settings from
the parent PRD belong here.
