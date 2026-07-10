# Console is replaced rather than migrated

Status: accepted

The new Console is a greenfield rewrite. At implementation start, the imported `apps/aiops_console_web` state is recorded in monorepo Git history and its `src/` tree is replaced; existing components, styles, state flows, route tree, and API wrappers are not copied or incrementally adapted. The old Console remains useful only as a feature inventory and does not define the new product behavior.

The rewrite has no legacy UI mode, compatibility components, parallel route tree, or feature flag. The new Console targets only the versioned `/api/v1/*` contract and current `/auth/*` semantics; after the replacement is accepted and deployed, the frozen unversioned Console API is removed as already required by ADR-0026. Git history is the rollback record, so no second archived source tree is maintained.
