# Approval may preauthorize one conditional rollback

Status: accepted

`批准并执行` may authorize both a primary mutation and one frozen Rollback Plan displayed on the approval surface. Gateway may trigger that rollback without another click only when the predefined post-check failure condition occurs and the target state still matches the approved assumptions. The rollback must be an allowlisted structured action included in the action hash; an Agent cannot invent or alter it after approval. Missing, stale, unsafe, or failed rollback stops execution in `rollback_required`, and every subsequent action requires a new explicit approval.
