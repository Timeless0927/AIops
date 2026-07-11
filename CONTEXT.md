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
A User authorized to manage platform identities, catalog data, Connector Enrollments, and Cluster policy without implicitly receiving Approval Authority.
_Avoid_: Approver, superuser

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
A durable operational case formed from correlated alerts and carrying its investigations, decisions, responsibility, and outcome through resolution.
_Avoid_: Alert, event, case

**Incident Report**:
A versioned, human-published account of one Incident assembled from frozen facts across all of its Investigations, decisions, actions, and recovery.
_Avoid_: Investigation report, Diagnosis export, Agent report

**Investigation**:
One bounded round of evidence gathering, reasoning, and human interaction within an Incident; an Incident has ordered Investigations and at most one may be active.
_Avoid_: Agent Run, Diagnosis Session, investigation thread

**Investigation Event**:
An immutable, ordered fact recorded during an Investigation and used to reconstruct its visible progress.
_Avoid_: SSE message, UI event, run event

**Evidence Step**:
An ordered, durable record of one evidence-acquisition attempt within an Investigation, including its purpose, outcome, and evidence references.
_Avoid_: Evidence Node, tool trace, raw observation

**Evidence Gate**:
Gateway's deterministic decision that current, scoped evidence satisfies the requirements for a particular Recommended Action to become approvable.
_Avoid_: AI confidence, human override, Approval

**Human Input**:
A User-contributed assertion, context update, or choice within an Investigation; it is not Evidence and grants no authority to change a Cluster.
_Avoid_: Fact, Evidence, Approval, confirmation to execute

**Recommended Action**:
An evidence-grounded operational proposal produced during an Investigation; it has no authority to change a Cluster.
_Avoid_: Command, remediation execution

**Approval**:
A human decision made through an explicit approval action on one frozen Recommended Action by a User whose authority covers the target environment and resource.
_Avoid_: Confirmation, Execution Grant

**Approval Authority**:
An explicit binding that permits a user to approve actions within the intersection of an Environment and a real resource scope.
_Avoid_: Platform admin, free-text scope

**Execution Grant**:
Gateway's short-lived, single-use authorization for one approved and frozen mutation against an exact resource scope.
_Avoid_: Approval, permission, Connector token

**Rollback Plan**:
A frozen conditional inverse action explicitly included in an Approval and permitted only when its predefined post-check failure occurs.
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
