# Console Next search/mobile/accessibility/smoke implementation plan

## Order

1. Start the task after this plan is present.
2. Read backend/frontend Trellis specs before editing.
3. Add the smallest Gateway-owned global search API that returns scoped results
   with real Console routes.
4. Add a Console search route that calls the Gateway API directly.
5. Tighten the mobile approval route for deep links, evidence summary, remarks,
   approve/reject, and execution status.
6. Add focused accessibility coverage for labels, keyboard/focus, status text,
   and empty/error/loading states in the touched routes.
7. Add one final Gateway-only smoke test for login, incident, agent run,
   timeline/evidence, action proposal, approval, mock execution, and audit chain.

## Validation

- `python3 -m py_compile apps/aiops_k8s_gateway/*.py`
- Focused Gateway tests for search, mobile approval, and final smoke.
- Existing approval, agent-run, audit, and Console route tests touched by this
  slice.
- `npm run build` in `apps/aiops_console_web`.
- `git diff --check`

## Rollback

Revert the child work commit for this task. Earlier child commits are
independent and should remain intact.
