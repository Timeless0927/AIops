# Console Next actions/approvals/execution implementation plan

## Order

1. Add a small Gateway action control service
   - Create `apps/aiops_k8s_gateway/action_control_service.py`.
   - Use SQLite WAL tables for `action_proposals`, `execution_grants`, and
     `target_locks`.
   - Normalize the three supported Kubernetes action types.
   - Reject missing or ambiguous targets before policy.
   - Build canonical command payloads for preflight, mutation, post-check, and
     rollback metadata.
   - Persist frozen action hash and idempotency replay.

2. Connect action proposal routes
   - `POST /api/actions/propose`: normalize, authorize by scope, classify policy,
     create approval or policy grant.
   - `GET /api/actions/{action_id}`: return proposal, grant, approval, execution,
     lock state, and responsibility refs.
   - Keep all browser calls same-origin Gateway `/api/*`.

3. Tighten approval -> automatic execution
   - On approve, create exactly one human approval grant.
   - Trigger execution through the existing execution service without requiring a
     second browser Execute click.
   - Preserve existing manual `/execute` compatibility for tests/service callers.
   - Enforce idempotency replay and grant hash matching.

4. Add target locks
   - Claim lock before dispatch.
   - Return conflict when another active execution owns the target.
   - Release lock on success, failure, preflight failure, and post-check failure.

5. Add Console pages
   - Extend `/approvals` and approval detail enough to show frozen action,
     target, risk, evidence refs, preflight, rollback, requester, grant,
     execution state, and remarks.
   - Add a minimal action request control from agent run detail or approvals flow.

6. Tests
   - Add `tests/test_gateway_actions_approvals_execution.py` with real
     `ThreadingHTTPServer` + `urllib`.
   - Cover missing/ambiguous target rejection, action hash, policy decision,
     approval-required path, policy grant path, one-time grant consumption,
     idempotency replay, target lock conflict, preflight failure, mutation
     failure, post-check rollback-required, audit, and timeline.
   - Extend `tests/test_aiops_console_web.py` for Gateway-only action/approval
     calls, CSRF writes, Chinese labels, and route guards.

## Validation commands

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/action_control_service.py apps/aiops_k8s_gateway/approval_service.py apps/aiops_k8s_gateway/approval_execution_service.py`
- `rtk test pytest -q tests/test_gateway_actions_approvals_execution.py`
- `rtk test pytest -q tests/test_gateway_approval_service.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk npm run build` from `apps/aiops_console_web`

## Review gates

- Browser calls Gateway only.
- `_authorize` runs before action/approval/execution data is returned or mutated.
- Missing resource scope fails closed for non-admin users.
- Frozen action hash is stable and verified before execution.
- One grant authorizes at most one execution.
- Locks are released for every terminal execution result.
- Connector remains the final command allowlist boundary.
- No new backend dependency, broker, worker, or host executor.

## Rollback points

- Action proposal routes and `action_control_service.py` are additive.
- Existing approval and execution routes remain compatible.
- Console approval detail can fall back to existing placeholder/detail state.

## Out of scope

- Host execution.
- Custom webhook execution.
- Multi-approver chains.
- New Connector command families beyond existing Kubernetes allowlist.
