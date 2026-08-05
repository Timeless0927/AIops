Type: grilling
Status: resolved
Blocked by: 01 (Establish Current Pilot Baseline)

# Define Generic Kubernetes Change Contract

## Question

What exact structured contract allows AIOps to propose, approve, and execute Kubernetes Changes across an entire registered Cluster, including system namespaces, without shell or free-form kubectl while preserving deterministic validation, exact human-visible diff, object preconditions, Approval Authority, audit, rollback, and Unknown Outcome handling?

The answer must define operation and resource policy, canonical create/patch/delete payloads, server-side dry-run, concurrency checks, sensitive data handling, post-check and inverse-change rules, Connector execution, and the minimum ClusterRole surface required by the Pilot Release Bundle.

## Comments

- Confirmed upstream: Cluster-wide scope includes system namespaces and is not limited to the verification fixture. The user explicitly requires AIOps to handle at least Deployments, StatefulSets, DaemonSets, Jobs, and changes such as creating a Job or changing a Service NodePort.
- Confirmed upstream: prefer one generic Kubernetes Change contract over adding one predefined action for every operation. Approval must freeze an exact structured diff; vague intent, arbitrary shell, and free-form kubectl remain unauthorized.
- Confirmed: authorization is tiered. Explicit Cluster Change Authority covers all namespaces, cluster-scoped resources, CRDs, and Sensitive Changes without being implied by Platform Administrator status. Small-team operation permits the same authorized User to submit the Change Request and approve the model-produced Kubernetes Change, including a Sensitive Change; fresh authentication, exact diff confirmation, short-lived Execution Grant, and audit remain mandatory. A second account is not required.
- Confirmed: Approval Authority supports explicit Object, Namespace, Service, and Cluster scopes, each intersected with Environment where applicable. Team Membership alone grants no mutation authority. Sensitive Changes always require Cluster Change Authority. Gateway revalidates the actor's real scope before proposal generation, diff visibility, Approval, Execution Grant issuance, and execution dispatch.
- Confirmed: a User does not author YAML, JSON Patch, or a Kubernetes Change proposal. The User submits a natural-language Change Request describing the desired outcome and context; the model alone produces the exact structured Kubernetes Change proposal. Gateway canonicalization, policy validation, live-object preconditions, and server-side dry-run precede User Approval. Model output remains non-authoritative.
- Confirmed: ambiguous target GVK/identity, desired state, scope, or post-check moves the Change Request to `needs_input`. The model asks one blocking question at a time and cannot generate an approvable Phase, guess defaults, or expand scope through an implicit selector. New User input produces a new immutable plan revision; every older revision becomes `superseded` and cannot be approved or executed.
- Confirmed: Gateway persists the Change Plan Phase state machine `planning -> needs_input|validating -> awaiting_approval -> approved -> executing`, followed by `paused`, `rolling_back`, `succeeded`, `failed`, `rolled_back`, or `rollback_failed`; `superseded`, `expired`, and pre-execution `cancelled` are side terminal states. Each step records `succeeded`, `failed`, `stale`, `unknown_outcome`, `effect_observed`, `rolled_back`, or `rollback_failed`. Change Request status is a projection of the active Phase, and every transition appends an immutable event.
- Confirmed: before any step is `started`, cancellation terminates the Phase and revokes unused Execution Grants. After `started`, Gateway records `cancel_requested`, issues no later grants, and still reconciles the current step to terminal or Unknown Outcome. Approval freezes `stop_only` or `rollback_completed`; the latter may execute only already-approved inverse changes. Irreversible Changes permit only `stop_only`.
- Confirmed: Gateway retains Change Request text, immutable plan revisions, redacted dry-run diffs, Authority/Approval/Execution Grant metadata, Phase/step events, actor/reason/request IDs, and execution/rollback/reconciliation outcomes as non-expiring governance history. Model reasoning traces, Secret plaintext, unredacted full objects, and raw API response bodies are not retained; Secure Input ciphertext is deleted after the rollback window. Connector removes acknowledged terminal journal records after 30 days in bounded cleanup but retains pending, Unknown Outcome, and unfinished rollback records. Obvious credentials in Change Request text are rejected before model submission and redirected to Secure Input.
- Confirmed: one Change Request may produce a multi-object Change Plan. One Approval freezes the ordered Kubernetes Changes. Connector executes sequentially, stops after the first failed or unknown step, and attempts each completed step's frozen inverse change in reverse order when its rollback condition applies. The product never describes this as an atomic Kubernetes transaction.
- Confirmed: every Kubernetes Change must pass Kubernetes API server-side dry-run before Approval. When an earlier step changes API discovery or validation, the plan splits into Change Plan Phases. After the prior phase reaches its post-check, the model refreshes live discovery, generates and dry-runs the next phase, and the User grants a separate Approval. Model-side schema validation never substitutes for API Server validation.
- Confirmed: Cluster Change Authority may approve an Irreversible Change when reliable rollback is impossible. The model must set `rollback: unavailable` and state the concrete irreversible effects. Console requires fresh authentication, a human reason, and re-entry of the exact target identity. Recreating a deleted object with a new UID or omitting destructive side effects is never presented as rollback.
- Confirmed: canonical execution payloads are complete JSON objects for create, RFC 6902 JSON Patch for patch, and Kubernetes DeleteOptions for delete. Every existing target freezes exact GVK, namespace/name, UID, and resourceVersion; patch includes `test` operations for identity/version and relevant old values, while delete uses UID/resourceVersion preconditions and an explicit propagation policy. Approval primarily shows the API Server dry-run final object diff.
- Confirmed: Secret plaintext never enters model context. A User supplies sensitive values through separate Secure Input, or Gateway generates them with a cryptographic RNG; the model receives only opaque placeholders. Gateway resolves placeholders from encrypted short-lived storage for canonicalization and dry-run, Approval exposes key names and value hashes only, Connector obtains the exact decrypted payload only for execution, and logs/audit/events/reports remain redacted.
- Confirmed: a dedicated `aiops-change-encryption` Kubernetes Secret provides a file-mounted AES-GCM key only to Gateway and Connector and is separate from Notification Engine encryption. Gateway/Connector databases and command journals persist ciphertext, nonce, and hash metadata only; plaintext is memory-only. Ciphertext is removed after a terminal Phase's rollback window while redacted audit facts remain. Key loss moves unfinished sensitive plans to `secure_input_unavailable` and forbids placeholder execution or credential regeneration.
- Confirmed: the Pilot Release Bundle installs a dedicated broad `aiops-change-executor` ClusterRole for Connector rather than binding the built-in `cluster-admin` role. No other AIOps process receives this credential. Projected short-lived ServiceAccount tokens, NetworkPolicy, structured API-only execution, exact Execution Grant/diff-hash validation, and durable audit are mandatory mitigations, but Connector compromise is explicitly accepted as capable of affecting the entire Cluster.
- Confirmed: Connector is the only Kubernetes API Adapter, including pre-Approval discovery, live-object reads, and server-side dry-run as well as execution. Gateway owns Change Request/Plan, canonical validation state, Approval, Execution Grant, and audit. Diagnosis/model receives sanitized facts and plans without Kubernetes credentials. Connector retains only its command journal and bounded results and never owns product decisions.
- Confirmed: immediately before execution, Connector revalidates frozen UID, resourceVersion, JSON Patch `test` operations, relevant old values, and Execution Grant/change hashes. Any drift returns `stale` without mutation. Gateway never rebases or automatically retries a mutation; the model must read current state, produce and dry-run a replacement, and obtain a new Approval. A stale later step stops its Change Plan Phase, with completed steps handled only by their pre-approved rollback conditions.
- Confirmed: only a trustworthy Connector journal terminal result records confirmed `succeeded` or `failed`. If no such result exists but reconciliation finds the exact expected state and all post-checks pass, Gateway records Observed Effect with unknown attribution; mismatch or ambiguity remains Unknown Outcome. Both pause the Change Plan and forbid mutation retry or automatic continuation. A User must explicitly accept reconciliation evidence before the model replans, and audit/reporting preserves the distinction from confirmed success.
- Confirmed: every Kubernetes Change freezes at least one structured Kubernetes API post-check. Allowed predicates cover existence/absence, identity/version, JSON Pointer comparisons, Kubernetes Conditions, observedGeneration, workload rollout readiness, Job terminal state, and CRD Established; optional Prometheus/Loki predicates must pass query guard. Shell, Python, and executable probes are forbidden. Post-check failure triggers rollback only when Approval froze an exact conditional inverse change; an Irreversible Change stops and records failure.
- Confirmed: fresh authentication is valid for 5 minutes; server-side dry-run and its Change Plan Phase remain approvable for 10 minutes; an approved Phase must start within 15 minutes. Gateway issues one 60-second single-use Execution Grant immediately before each step, and Connector's 30-second Command Lease governs claim only. After `started`, the frozen execution timeout is 5 minutes by default and at most 30 minutes. A later grant is issued only after the prior step reaches trustworthy terminal state and passes post-check. Conditional rollback authority is frozen with the original step and remains valid through its execution timeout; timeout without a terminal result becomes Unknown Outcome.
- Confirmed: `aiops-change-executor` uses `apiGroups: ["*"]`, `resources: ["*"]`, and only `get`, `list`, `watch`, `create`, `patch`, and `delete`, plus GET-only discovery non-resource URLs. It omits `update`, `deletecollection`, `impersonate`, `bind`, `escalate`, and non-resource writes. Product policy rejects subresource operations even though wildcard RBAC means a compromised Connector credential may reach them; that residual risk is accepted.

## Answer

采用模型生成、人工审批 exact diff、Connector 结构化执行的通用 Kubernetes Change contract，替代逐项预定义 `restart_deployment` 等 remediation action。该 contract 覆盖整个注册 Cluster，包括系统 namespace、内置资源、cluster-scoped resources 和 CRD；不允许 shell、自由 kubectl 或由 User 编写 YAML/JSON Patch。

### Owner 与主流程

1. User 用自然语言提交 Change Request；明显 credential 必须改走 Secure Input。
2. Diagnosis/model 通过 Connector 返回的脱敏 discovery/live facts 生成 immutable Change Plan；模糊请求进入 `needs_input`，一次询问一个阻塞问题。
3. Gateway canonicalize 每个 Kubernetes Change，校验 Authority/policy/preconditions，并通过 Connector 请求 API Server dry-run。
4. Console 展示 dry-run 后的 exact object diff、风险、post-check、rollback 与不可逆后果。
5. User 以 fresh auth 显式审批整个 Change Plan Phase；小团队中同一 User 可提交请求并自我审批。
6. Gateway 为每个 step 即时签发单次 Execution Grant；Connector 是唯一 Kubernetes API Adapter，按顺序执行并持久化 journal/result。
7. Gateway 记录 post-check、rollback、Unknown Outcome/reconciliation 和 immutable events；模型输出从不构成执行授权。

Gateway 拥有 Change Request/Plan、Evidence Gate、Approval、Execution Grant、状态与审计。Connector 只拥有 Kubernetes transport、command journal 和 bounded result；Diagnosis/model、Gateway 和 Console 都不持有 Kubernetes credential。

### Canonical contract

| Operation | Frozen payload | Required precondition |
| --- | --- | --- |
| `create` | 完整 canonical JSON object | exact GVK/namespace/name 尚不存在 |
| `patch` | RFC 6902 JSON Patch | UID、resourceVersion、相关 old values 的 `test` operations |
| `delete` | Kubernetes DeleteOptions | UID、resourceVersion、明确 propagation policy |

Approval 主要展示 API Server dry-run 后的 final object diff。strategic merge patch、server-side apply、自由 manifest 和 subresource operation 不进入 contract。执行前任一 UID、resourceVersion、old-value 或 change hash 漂移都会产生 Stale Change；系统不 rebase、不自动 mutation retry，必须重新读取、生成、dry-run 和审批。

一个 Change Request 可以生成多对象 Change Plan，但 Kubernetes 不具备跨对象事务。Connector 顺序执行，首个 failed/stale/unknown step 即停止。所有 step 可在审批前 dry-run 时，共用一个 Phase Approval；若前一步改变 API surface，例如先创建 CRD 再创建 Custom Resource，则必须拆成 Change Plan Phases，前一 Phase 完成后重新 discovery、生成、dry-run 和审批。

### Authority 与风险

Approval Authority 支持 Object、Namespace、Service、Cluster 四级 scope，并与 Environment 相交。Team Membership 和 Platform Administrator 身份不隐含 mutation authority。Sensitive Change 始终要求显式 Cluster Change Authority；该 Authority 覆盖所有 namespace、cluster-scoped resources、CRD 和敏感资源。

Pilot 面向小团队，持有 Cluster Change Authority 的单一 User 可以自我审批，包括 Sensitive Change；仍必须 fresh auth、填写原因、确认 exact diff，并由 Gateway 在 proposal、diff access、Approval、grant issuance 和 dispatch 时重复校验 scope。Irreversible Change 可以审批，但必须声明 `rollback: unavailable` 和具体损失，并要求重新输入 exact target identity；不得伪造 rollback。

### Post-check、rollback 与 cancellation

每个 Kubernetes Change 至少冻结一个结构化 Kubernetes API post-check。允许 existence/absence、identity/version、JSON Pointer comparison、Condition、observedGeneration、workload rollout、Job terminal、CRD Established，以及通过 query guard 的 Prometheus/Loki predicate；禁止脚本或可执行 probe。

Rollback Plan 只包含 Approval 已冻结的 exact inverse changes。post-check failure 仅在匹配预定义 condition 时触发；Irreversible Change 停止并记录失败。执行前可直接 cancel 并撤销 grant；step `started` 后只能记录 `cancel_requested`、停止签发后续 grant，并等待可信 outcome。Approval 预先选择 `stop_only` 或 `rollback_completed`，Irreversible Change 只能 `stop_only`。

### Unknown Outcome

只有可信 Connector journal terminal result 才记录 confirmed `succeeded`/`failed`。没有 terminal result 但 live state 与全部 post-check 匹配时记录 Observed Effect，不能冒充 success；不匹配或无法判断时保持 Unknown Outcome。两者都暂停计划、禁止 retry/自动继续，User 接受 reconciliation evidence 后模型才能基于最新状态重新规划。

### Sensitive data

模型永不接收 Secret plaintext。User 通过 Secure Input 提供，或由 Gateway CSPRNG 生成；模型仅引用 opaque placeholder。Gateway/Connector 使用独立 file-mounted `aiops-change-encryption` key 对 gateway.db、connector.db 和 command journal 中的 sensitive payload 做 AES-GCM encryption。Approval、日志、审计、event 和 report 只暴露 key name/hash；plaintext 仅短暂存在于内存。

terminal Phase 的 rollback window 结束后删除 ciphertext，长期保留 redacted facts。key 丢失时未完成敏感计划进入 `secure_input_unavailable`，不得用 placeholder 执行或重新生成 credential。该 key 不与 Notification Engine 共用。

### State 与时限

Phase state 为 `planning -> needs_input|validating -> awaiting_approval -> approved -> executing`，随后进入 `paused`、`rolling_back` 或 terminal state；side terminal state 包括 `superseded`、`expired`、`cancelled`。Step outcome 为 `succeeded`、`failed`、`stale`、`unknown_outcome`、`effect_observed`、`rolled_back`、`rollback_failed`。Change Request status 由 active Phase 投影，每次转换追加 immutable event。

固定时限：fresh auth 5m，dry-run/Phase approval window 10m，approved Phase start window 15m，每 step Execution Grant 60s，Command Lease 30s，started execution timeout 默认 5m、最大 30m。后续 grant 仅在前一步可信 terminal 且 post-check 通过后签发；无 terminal 的执行超时进入 Unknown Outcome。

### RBAC 与 accepted risk

Pilot Release Bundle 只给 Connector 安装专用 `aiops-change-executor` ClusterRole：

```yaml
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["get", "list", "watch", "create", "patch", "delete"]
  - nonResourceURLs: ["/api", "/api/*", "/apis", "/apis/*", "/openapi", "/openapi/*", "/version"]
    verbs: ["get"]
```

不授予 `update`、`deletecollection`、`impersonate`、`bind`、`escalate` 或 non-resource write。Projected short-lived token、NetworkPolicy、structured API-only execution、Execution Grant/hash validation 和 durable audit 是强制缓解措施；但 wildcard RBAC 在 credential 层也覆盖 subresources，Connector compromise 可能绕过 Gateway 并影响整个 Cluster。该风险已显式接受，详见 [ADR-0053](../../../docs/adr/0053-generic-kubernetes-change-contract.md)。

### Retention

Gateway 不自动删除 Change Request、immutable plan revisions、redacted dry-run diffs、Authority/Approval/Grant metadata、events 和所有 execution/rollback/reconciliation outcome。Reasoning trace、Secret plaintext、unredacted full object、raw API body 不长期保留。Connector acknowledged terminal journal 在 30 天后有界清理；pending、Unknown Outcome 和 unfinished rollback 不清理。
