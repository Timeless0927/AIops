Type: wayfinder:map
Status: open

# AIOps Pilot-ready Release Wayfinder

## Destination

Produce an agent-ready decision set for a Pilot-ready Release that a new Platform Operator can deploy from versioned Kubernetes artifacts into one real non-production Cluster, after which Users can complete the real Alert Signal-to-Incident Report workflow without seeded product state or direct database manipulation.

The accepted workflow includes real bundled metrics and logs, Web-configured model and notification integrations, a controlled Alert Signal, evidence-grounded diagnosis, an explicitly approved isolated restart, recovery, notification, and Incident Report publication.

## Notes

- Planning only: resolve decisions, do not implement the destination in this map.
- Human-facing project documents remain Chinese; code identifiers, paths, commands, API fields, and error codes remain English.
- HITL questions use Chinese domain terms followed by their English term or identifier so decisions remain readable and traceable to contracts.
- Preserve the V1 domain and safety model in `CONTEXT.md` and `.scratch/aiops-v1-model/`: team/resource-scoped visibility, explicit Approval Authority, deterministic Evidence Gate, explicit Approval, no authorization from model output, and no automatic mutation retry.
- Pilot topology is one Kubernetes Cluster: control plane and the managed non-production workload may share the Cluster while remaining isolated by namespace and RBAC. Multi-Cluster onboarding comes later.
- Product installation is Kubernetes-only and Kustomize-only, using versioned release manifests and immutable image digests. Source checkout, local builds, Helm, and Docker Compose are not product install paths.
- Internal service traffic remains HTTP. Ingress TLS is supported but optional for Pilot acceptance.
- Platform Operator owns Kubernetes installation. Platform Administrator owns post-login external integration configuration. SRE owns Incident work. These responsibilities do not imply one User per role.
- The Pilot supports small teams with one operational User. A User holding explicit Cluster Change Authority may self-approve, including Sensitive Changes; fresh authentication, an exact frozen diff, explicit execution confirmation, and audit remain mandatory, and model output never grants authority.
- Approval Authority has explicit Object, Namespace, Service, and Cluster scopes intersected with Environment. Team Membership grants no mutation authority; Sensitive Changes always require Cluster Change Authority, and Gateway revalidates scope during proposal, diff access, Approval, and execution.
- A User submits a Change Request as desired outcome and context, not YAML, JSON Patch, or an executable proposal. The model produces the exact Kubernetes Change; Gateway validation and server-side dry-run make its diff approvable, while only the User can grant execution authority.
- Ambiguous target, desired state, scope, or post-check moves a Change Request to `needs_input`; the model asks one blocking question at a time and cannot produce an approvable Phase or expand scope through an implicit selector. New input creates a new immutable plan revision and supersedes the old one.
- Gateway persists one Phase state machine (`planning`, `needs_input`, `validating`, `awaiting_approval`, `approved`, `executing`, `paused`, rollback and terminal states) plus explicit step outcomes. Change Request status is a projection, and every transition appends an immutable event.
- Cancellation before `started` revokes unused grants. After `started`, `cancel_requested` stops future grants but cannot interrupt or hide the current outcome; Approval freezes either `stop_only` or `rollback_completed`, while Irreversible Changes permit only `stop_only`.
- Gateway retains Change Request, immutable plan revisions, redacted diffs, authority/approval/grant metadata, events, and all outcomes as governance history. Reasoning traces, plaintext secrets, unredacted objects, and raw API bodies are not retained; acknowledged Connector journals clean after 30 days while unfinished records remain.
- One Change Request may produce a multi-object Change Plan. Approval freezes its ordered Kubernetes Changes; Connector executes them sequentially, stops on failure, and attempts frozen inverse changes for completed steps without claiming cross-object atomicity.
- Every Kubernetes Change must pass API Server dry-run before Approval. Dependencies that change the Kubernetes API surface split a Change Plan into phases; after the prior phase completes, the model refreshes discovery and the User separately approves the next validated phase.
- Cluster Change Authority may approve an Irreversible Change. It must declare `rollback: unavailable` and concrete loss; fresh authentication, a required reason, and re-entry of the exact target identity replace any false rollback promise.
- Kubernetes Change payloads are canonical: create freezes a complete JSON object, patch uses RFC 6902 JSON Patch, and delete uses Kubernetes DeleteOptions. UID/resourceVersion preconditions are mandatory, and Approval presents the API Server dry-run object diff.
- Models never receive Secret plaintext. Secure Input supplies or generates sensitive values outside the Change Request, models reference opaque placeholders, Gateway injects encrypted values, and all visible diffs and durable records expose only key names and value hashes.
- Gateway and Connector encrypt Secure Input journals with a dedicated file-mounted `aiops-change-encryption` key, separate from Notification Engine. Plaintext is memory-only, ciphertext is deleted after terminal rollback windows, redacted hashes remain auditable, and key loss stops pending sensitive plans as `secure_input_unavailable`.
- The Pilot Release Bundle grants only Connector a dedicated broad `aiops-change-executor` ClusterRole so it can execute generic Kubernetes Changes. Projected tokens, NetworkPolicy, exact Execution Grant validation, and audit reduce exposure but do not remove the accepted fact that Connector compromise can affect the entire Cluster.
- Connector is the only Kubernetes API Adapter for discovery, live reads, server-side dry-run, and execution. Gateway owns Change Request/Plan, validation state, Approval, Execution Grant, and audit; Diagnosis/model plans without Kubernetes credentials, and Connector owns no product decision.
- Execution revalidates every frozen UID, resourceVersion, old-value test, and change hash. Drift produces a Stale Change with no mutation or automatic retry; the model must replan from live state and obtain a new dry-run and Approval.
- Unknown Outcome reconciliation distinguishes a trustworthy terminal result from Observed Effect. Matching live state without execution attribution pauses the plan as Observed Effect until explicit User acceptance; ambiguous state remains Unknown Outcome, and neither state triggers automatic mutation retry.
- Every Kubernetes Change freezes at least one structured Kubernetes API post-check; optional Prometheus/Loki predicates must pass query guard. Executable probes are forbidden, and post-check failure triggers only an exact inverse change frozen by Approval.
- Time bounds are fixed: fresh auth 5m, dry-run/Phase approval window 10m, approved Phase start window 15m, per-step single-use Execution Grant 60s, claim lease 30s, and frozen started execution timeout 5m by default with a 30m maximum. Later grants are issued only after prior terminal post-checks.
- The Web setup flow is optional and resumable. Skipped capabilities remain visibly unconfigured or unverified; the product must not create fake data or report false readiness.
- Pilot defaults to real bundled Prometheus/Loki collection. Existing external observability backends remain a later or optional Adapter path.
- A real OpenAI-compatible model and one real Notification Destination are configured after login through the Console, not through the deployment happy path.
- Pilot acceptance exercises mutation in a verification namespace, but product scope is Cluster-wide, including system namespaces. A Kubernetes Change is an exact, immutable structured API change rather than a predefined remediation action or free-form command, and still requires the Evidence Gate, explicit Approval, Execution Grant, and Connector Command chain.
- Stateful services remain single-replica with independent PVCs. Restart and rolling-update recovery are required; HA, PostgreSQL, and automated backup are not.
- Use `/grilling` and `/domain-modeling` for HITL decisions and `/prototype` for the setup/platform-status UX ticket.

## Decisions so far

- [Establish Current Pilot Baseline](issues/01-establish-current-pilot-baseline.md) — HEAD 的 V1 contract 代码与 fake-backed tests 已覆盖主流程；干净集群尚无任何 AIOps 部署或真实工作流证据，完整 immutable release、真实 bundled observability、Web model/setup readiness 仍缺失。
- [Define Generic Kubernetes Change Contract](issues/09-define-generic-kubernetes-change-contract.md) — User 以自然语言请求、模型生成 exact Change Plan、Gateway dry-run/审批、Connector 以广权限执行结构化 Cluster-wide change，并显式接受 Connector compromise 的全 Cluster 风险。
- [Define Model And Notification Readiness](issues/05-define-model-and-notification-readiness.md) — readiness 绑定 exact revision verification 与 live availability；Model 以真实 tool-use/JSON probe 验证且失败不回退 keyword，Notification 以真实 durable test Delivery 验证，完整 Pilot 验收不能用 skip 替代。
- [Define Bundled Observability And Alert Path](issues/04-define-bundled-observability-and-alert-path.md) — canonical Kustomize bundle 使用真实 Prometheus、Alertmanager、single-binary Loki、kube-state-metrics 与 Alloy，复用现有 monitor 的已调配置但移除 Helm/Operator/私有环境耦合；只有真实 scrape、log tail、rule、webhook 和 MCP evidence 可计入验收。
- [Define Controlled Verification Scenario](issues/06-define-controlled-verification-scenario.md) — optional Kustomize fixture 以两轮真实 latched readiness fault 验证 Alert、Model/MCP Evidence、namespace-scoped Approval、Connector rollout、Recovery、两版 Report 与 resolved Notification；不 seed product state、不自动恢复或隐藏 retry。

## Not yet specified

- Whether the existing broad `/admin` surface should remain one page or split into focused settings areas depends on the setup and Platform Status prototype.
- Additional operator recovery controls cannot be named until the controlled end-to-end scenario exposes the real failure points.

## Out of scope

- Multi-Cluster onboarding and fleet management.
- Production Cluster mutation acceptance.
- HA control-plane replicas, PostgreSQL migration, automated backup, and disaster recovery.
- Mandatory external HTTPS, internal TLS, or mTLS.
- Helm and Docker Compose product installation paths.
- Seeded Incident state, direct database demo scripts, fake AI, and synthetic product responses as acceptance evidence.
- Replacing the V1 authorization, Approval, Connector Command, Notification, or Incident ownership model without a separately justified architecture decision.
