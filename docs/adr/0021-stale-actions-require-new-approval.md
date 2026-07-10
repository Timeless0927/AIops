# Changed or stale actions require new approval

Status: accepted

Each Recommended Action version is immutable and identified by an action hash covering its target, parameters, evidence basis, safeguards, and rollback. New evidence or changed parameters create a new version and supersede the old one. `批准并执行` revalidates the exact version the User viewed against current evidence, Resource Binding, target state, and expiry; any mismatch returns `action_stale` without recording Approval or issuing an Execution Grant. A User must review and explicitly approve the replacement version, and every Execution Grant is short-lived and single-use.
