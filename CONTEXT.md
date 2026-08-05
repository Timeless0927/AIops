# AIOps Control Plane

AIOps coordinates Kubernetes incident diagnosis and governed operations across the clusters of one organization.

## Language

**Gateway**:
The central control plane that owns incident state, authorization, governance, and routing across registered Clusters.
_Avoid_: K8s Gateway, cluster gateway

**Connector**:
The trusted representative of exactly one Cluster, responsible for reporting cluster facts and performing Gateway-authorized operations.
_Avoid_: Agent, cluster client

**Connector Enrollment**:
A pre-authorized binding between one Connector identity and one Cluster identity; it grants permission to register but does not make the Cluster present in AIOps.
_Avoid_: Manual cluster creation, shared connector account

**Connector Registration**:
The authenticated presence established when an enrolled Connector connects to Gateway, making its Cluster known to AIOps.
_Avoid_: Cluster configuration

**Connector Command**:
A Gateway-owned durable instruction assigned to exactly one Connector for one bounded read or approved mutation.
_Avoid_: Command Task, direct kubectl request

**Command Lease**:
A short-lived exclusive claim that allows one Connector to accept a Connector Command; it does not itself authorize the underlying mutation.
_Avoid_: Execution Grant, Connector lock

**Unknown Outcome**:
The state of a Connector Command whose execution started but whose trustworthy terminal result has not arrived; it requires reconciliation and forbids automatic mutation retry.
_Avoid_: Failed command, timeout retry

**Observed Effect**:
A reconciliation result where live state and post-checks match an approved Kubernetes Change but no trustworthy terminal execution result proves attribution. It is not confirmed execution success and requires explicit User acceptance before replanning continues.
_Avoid_: Reconciled success, inferred execution, successful command

**Cluster**:
A Kubernetes control plane recognized by AIOps only through an authenticated Connector registration.
_Avoid_: Manually configured cluster

**Environment**:
An operational risk domain such as production, staging, development, or test that constrains policy and approval authority.
_Avoid_: Namespace, Cluster

**Team**:
The organizational group accountable for one or more Services.
_Avoid_: User group, namespace owner

**User**:
A human identity authenticated by AIOps and authorized through Team Memberships and Role Bindings.
_Avoid_: Service Identity, Connector identity

**Team Membership**:
The relationship placing a User in a Team without by itself granting operational or approval authority.
_Avoid_: Role, permission

**Role Binding**:
An explicit assignment of a human role to a User within a defined organizational and resource scope.
_Avoid_: Free-text scope, implicit admin permission

**Platform Administrator**:
A User authorized to manage platform identities, catalog data, external integration configuration, Connector Enrollments, and Cluster policy without implicitly receiving Approval Authority.
_Avoid_: Approver, superuser

**Platform Operator**:
The person who installs and operates the AIOps control-plane processes and connects their infrastructure dependencies; this infrastructure responsibility does not itself grant Console authorization.
_Avoid_: Platform Administrator, SRE, approver

**Service**:
An operationally meaningful workload identity owned by one Team and realized in one or more Deployment Targets.
_Avoid_: Kubernetes Service object, arbitrary alert label

**Deployment Target**:
A concrete realization of a Service in one Cluster and namespace, identified by its workload or service identity.
_Avoid_: Cluster, deployment name

**Resource Catalog**:
The AIOps-owned registry of Teams, Services, Deployment Targets, and their confirmed relationships.
_Avoid_: CMDB, alert labels

**Discovery Candidate**:
A real workload or service observed by a Connector that may be promoted or linked within the Resource Catalog.
_Avoid_: Service, permanent binding

**Resource Binding**:
The confirmed relationship linking a Deployment Target to a Service and its owning Team for authorization and policy decisions.
_Avoid_: Label match, inferred ownership

**Alert Signal**:
One fingerprinted alert instance attached to an Incident as evidence of an observed operational symptom.
_Avoid_: Incident, notification

**Recovery Observation**:
The recorded fact that every Alert Signal currently attached to an Incident is resolved; it begins stabilization but is not itself Incident resolution.
_Avoid_: Resolved Incident, closed alert

**Incident**:
A durable operational case formed from correlated alerts or an explicit User report and carrying its investigations, decisions, responsibility, and outcome through resolution.
_Avoid_: Alert, event, case

**User-created Incident**:
An Incident created by an explicit User handoff for an operational problem without a correlated Alert Signal; it requires a real resource scope and remains subject to the same Investigation and governance rules.
_Avoid_: Manual ticket, free-form Agent Run

**Incident Report**:
A versioned, human-published account of one Incident assembled from frozen facts across all of its Investigations, decisions, actions, and recovery.
_Avoid_: Investigation report, Diagnosis export, Agent report

**Investigation**:
One bounded round of evidence gathering, reasoning, and human interaction within an Incident; an Incident has ordered Investigations and at most one may be active.
_Avoid_: Agent Run, Diagnosis Session, investigation thread

**Chat Session**:
A User interaction for general questions and read-only operational assistance that is separate from an Incident Investigation; its messages are not Evidence, Approval, or execution authority.
_Avoid_: Investigation, Agent Run

**Investigation Handoff**:
An explicit User action that attaches a Chat Session context to an existing Incident or creates a User-created Incident and its first Investigation; it copies selected context as Human Input and does not promote chat content to Evidence.
_Avoid_: Automatic diagnosis, chat-to-execution

**Investigation Event**:
An immutable, ordered fact recorded during an Investigation and used to reconstruct its visible progress.
_Avoid_: SSE message, UI event, run event

**Evidence Step**:
An ordered, durable record of one evidence-acquisition attempt within an Investigation, including its purpose, outcome, and evidence references.
_Avoid_: Evidence Node, tool trace, raw observation

**Decision Trace**:
The User-visible, structured account of an Investigation's goals, tool activity, evidence impact, and stopping reason; it excludes model-private reasoning and raw Chain of Thought.
_Avoid_: Chain of Thought, debug trace, raw model transcript

**Evidence Gate**:
Gateway's deterministic decision that current, scoped evidence and live validation satisfy the requirements for a Recommended Action or Change Plan Phase to become approvable.
_Avoid_: AI confidence, human override, Approval

**Human Input**:
A User-contributed assertion, context update, or choice within an Investigation; it is not Evidence and grants no authority to change a Cluster.
_Avoid_: Fact, Evidence, Approval, confirmation to execute

**Change Request**:
A User-stated operational outcome and context submitted for model planning outside or alongside an Investigation. It is not a Kubernetes Change, Evidence, Approval, or execution authority.
_Avoid_: User proposal, manifest, command, Human Input

**Secure Input**:
A sensitive value supplied by a User or generated by Gateway that a model references only through an opaque placeholder. Its plaintext is excluded from model context, visible diffs, logs, audit records, and reports.
_Avoid_: Prompt secret, YAML value, redacted Human Input

**Recommended Action**:
An evidence-grounded operational proposal produced during an Investigation; it has no authority to change a Cluster.
_Avoid_: Command, remediation execution

**Approval**:
A human decision made through an explicit approval action on one frozen Recommended Action or Change Plan Phase by a User whose authority covers the target environment and resources.
_Avoid_: Confirmation, Execution Grant

**Approval Authority**:
An explicit binding that permits a User to approve actions within the intersection of an Environment and a real resource scope.
_Avoid_: Platform admin, free-text scope

**Cluster Change Authority**:
An explicit binding that permits a User to approve Kubernetes Changes throughout one Cluster, including system namespaces, cluster-scoped resources, and Sensitive Changes. It is never implied by Platform Administrator status.
_Avoid_: Superuser, cluster admin role, implicit platform authority

**Sensitive Change**:
A Kubernetes Change whose target or effect can alter cluster identity, authorization, API behavior, credentials, or control-plane availability.
_Avoid_: Dangerous command, admin operation, high-risk intent

**Irreversible Change**:
A Kubernetes Change whose effects cannot be reliably restored from a frozen inverse change, including destructive side effects or recreated object identity. It remains approvable only when its unavailable rollback and concrete loss are explicit.
_Avoid_: Best-effort rollback, risky change, forced operation

**Stale Change**:
An approved Kubernetes Change whose live target no longer matches its frozen identity, version, or old-value preconditions. It cannot execute or rebase and must be replaced by a newly planned and approved change.
_Avoid_: Retryable conflict, outdated patch, auto-rebased change

**Execution Grant**:
Gateway's short-lived, single-use authorization for one approved and frozen mutation against an exact resource scope.
_Avoid_: Approval, permission, Connector token

**Kubernetes Change**:
A model-produced, immutable create, patch, or delete proposal against one exact Kubernetes object, including its preconditions, validated diff, post-check, and rollback. It gains execution authority only through an explicit Approval and matching Execution Grant.
_Avoid_: Typed remediation action, free-form kubectl, shell command, approved intent

**Change Plan**:
An ordered, immutable set of model-produced Kubernetes Changes approved together and executed sequentially. It stops on failure and may apply frozen inverse changes to completed steps, but it is not an atomic transaction.
_Avoid_: Kubernetes transaction, batch apply, manifest bundle

**Change Plan Phase**:
The largest ordered portion of a Change Plan whose Kubernetes Changes have all passed server-side dry-run before one Approval. A dependency that changes the Kubernetes API surface begins a new phase after live discovery and requires another Approval.
_Avoid_: Unvalidated plan step, hidden continuation, model-validated phase

**Rollback Plan**:
A frozen conditional set of inverse Kubernetes Changes explicitly included in an Approval and permitted only under its predefined failure or cancellation condition.
_Avoid_: Agent-generated recovery, new Recommended Action

**Diagnosis Request**:
Gateway's durable commitment that an Investigation requires Diagnosis execution, pending until Diagnosis accepts it or it ends as rejected, expired, or cancelled.
_Avoid_: Fire-and-forget handoff

**Diagnosis Job**:
Diagnosis's durable execution of one accepted Diagnosis Request, uniquely identified by its session ID.
_Avoid_: Diagnosis Session, background thread

**Service Identity**:
The authenticated identity assigned to one internal AIOps process and used to authorize calls between control-plane processes.
_Avoid_: Shared internal token, Connector identity

**Console**:
The browser-based AIOps interface through which Users investigate Incidents, administer platform state, and explicitly approve governed actions.
_Avoid_: Gateway UI, admin panel

**Model Provider**:
The single OpenAI-compatible model endpoint and credential configuration owned by Diagnosis and bound to a verified revision for new Diagnosis Jobs.
_Avoid_: LLM environment, model fallback, provider registry

**MCP Integration**:
An administrator-governed connection to one MCP server whose available tools, health, allowed scope, and enablement are verified by AIOps policy.
_Avoid_: Untrusted tool endpoint, browser MCP connection

**Skill**:
A versioned instruction or workflow definition that may reference enabled MCP tools but cannot execute arbitrary code or expand User authority.
_Avoid_: Plugin, executable package, permission grant

**Web Setup**:
The optional, resumable Platform Administrator workflow that presents and changes owner-held integration state without owning a separate completion state.
_Avoid_: Setup wizard state, onboarding tour, mandatory first run

**Platform Status**:
The Gateway-aggregated capability view of owner-held configuration, verification, connectivity, and availability; it is not a setup completion flag.
_Avoid_: Setup status, global healthy flag

**Pilot-ready Release**:
An AIOps release that a new User can deploy against one real non-production Cluster and its real observability backends, then complete the Incident-to-Report workflow without seeded product state or direct database manipulation.
_Avoid_: Demo mode, mock-data showcase, production-ready release

**Clean Acceptance Run**:
One promotion-eligible evaluation of one immutable Pilot Release Bundle on one clean non-production Cluster, whose evidence remains continuous and whose external effects are never replayed; Acceptance Runner process interruption is allowed only when durable public facts prove safe continuation of the same gate. A hash-bound, no-I/O Evaluator Correction may revise a tool judgment without changing the Product or replaying a gate effect.
_Avoid_: Diagnostic continuation, retried acceptance, uninterrupted process

**Evaluator Correction**:
An append-only replacement of an Acceptance Runner assertion over already hash-bound evidence. It preserves the original failed fact, records the old and new evaluator identities, performs no Product or external I/O, and may restore the frontier only before eligibility evaluation or sealing.
_Avoid_: Gate retry, evidence recollection, mutation replay, product remediation

**Failure Attribution**:
The bounded explanation attached to an ineligible Clean Acceptance Run: Product Failure, Acceptance Tool Failure, Environment Failure, or Inconclusive; it never changes the failed gate or restores promotion eligibility.
_Avoid_: Gate status, cleanup trigger, retry reason

**Deployment Disposition**:
The derived decision to retain an exact deployed Pilot Release Bundle or require rebuild after an ineligible run; it is independent of promotion eligibility.
_Avoid_: Gate result, automatic cleanup, retry permission

**Deployment Continuation Epoch**:
A signed, checksummed bridge from one sealed no-promote run to one replacement Acceptance Runner freeze, proving an unchanged product and Cluster plus unique public reconciliation of every issued mutation; it transfers no gate result.
_Avoid_: Reopened ledger, inherited evidence, resumed acceptance

**Existing Deployment Adoption**:
The read-only I01 path that verifies an exact, healthy existing Pilot Release Bundle for a new Clean Acceptance Run without applying it again.
_Avoid_: Reused I01 evidence, same-ledger retry, best-effort deployment reuse

**Acceptance Runner**:
The local, non-product tool that advances one Clean Acceptance Run gate at a time, coordinates fixed Operator and Console actions, and writes the evidence ledger without owning product state or promotion authority.
_Avoid_: Runner, Agent, control-plane service

**Diagnostic Evidence Bundle**:
An append-only troubleshooting record linked to one failed Clean Acceptance Run, its candidate, and its failed gate; it cannot add gate results or change promotion eligibility.
_Avoid_: Acceptance evidence, retry attempt, promotion evidence

**Promotion Decision**:
The signed release-owner decision to promote or not promote one evaluated Pilot Release Bundle; technical eligibility is required for promotion but never performs or authorizes it automatically.
_Avoid_: Promotion eligibility, automatic release, final gate status

**Pilot Release Bundle**:
A versioned, checksummed artifact containing the self-contained Kustomize overlay and immutable image references for one Pilot-ready Release.
_Avoid_: Source archive, deployment repository, Helm chart

**Notification Engine**:
The independent AIOps service that accepts Notification Requests, evaluates routing policy, and manages channel deliveries without changing Incident, Approval, or execution state.
_Avoid_: Alert engine, Feishu bot, Gateway notifier

**Notification Request**:
A durable, channel-neutral request to communicate one AIOps domain event to configured recipients.
_Avoid_: Alert Signal, Notification Delivery, direct message

**Notification Delivery**:
The channel-specific delivery lifecycle created by Notification Engine for one Notification Request and one configured destination.
_Avoid_: Notification Request, Incident Event

**Notification Route**:
An ordered policy that matches a Notification Request and either suppresses it with a reason or fans it out to one or more configured destinations.
_Avoid_: Alert rule, script, recipient override

**Notification Destination**:
A configured provider endpoint and recipient set referenced by Notification Routes without exposing its credentials.
_Avoid_: Raw webhook, chat ID, recipient override

**Notification Template**:
A versioned, provider-specific presentation that renders one typed Notification Request without choosing its recipients or credentials.
_Avoid_: Notification Route, arbitrary message script

**Notification Silence**:
A time-bounded, scoped suppression of matching Notification Requests with a required human reason and audit trail.
_Avoid_: Disabled route, dropped alert, quiet hours
