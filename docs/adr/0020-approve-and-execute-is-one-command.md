# Approval and execution submission are one explicit command

Status: accepted

Console presents one consequence-labelled `批准并执行` action rather than separate approve and execute controls. Gateway handles it as one idempotent command: it revalidates Approval Authority, binding, policy, evidence, and Connector availability, then atomically records the Approval, creates a single-use Execution Grant, and persists the Connector command. If preconditions or command persistence fail, none of those records is committed. Subsequent preflight or execution failure remains an audited result of the submitted command rather than silently undoing the Approval.
