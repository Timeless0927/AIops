Type: spec
Status: ready-for-agent

# 可信 Alert-to-Report 收口与 Replacement Clean Acceptance

## Problem Statement

现有 Pilot acceptance 已通过多轮 live diagnostic 暴露并修复大量真实边界问题，但当前证据、代码和执行顺序仍不能支持可信 promotion：产品事实、Acceptance Runner 判定和 Promotion Decision 混在一起；gate 顺序分散且把 R05 错放在 V05 之后；A01 可以在缺少 R/V/C 时 finalize；V01-V07 允许以历史 passed attempt 越过 retained failure；R01-R06、V08 和 C01-C03 尚无完整 Gate Module；运行中修脚本、补录 gate 或重复只读 gate 仍可能被描述为 accepted。

当前 `v0.1.0-20260715T092231Z` 及更早 evidence 包含 retained failed attempts、observe-only 补录和重复 passed gate，只能作为 Diagnostic Evidence Bundle，不能证明同一 immutable candidate 在同一 clean Cluster 上完成了一条连续、无重放的 promotion path。若现在继续 live run，新的产品修复、Acceptance Runner 修复和 Promotion Contract 仍会在验收途中交叉变化，无法达到“所有修复一次收口后，只做一次 Clean Acceptance Run”的目标。A10 与 A20 均已以 sealed `failed_no_promote` 终止；两个 ledger 都保持 immutable diagnostic，不得因事后 Cluster 清理而重试或改写。

## Solution

先离线收口三个相互独立的 contract，再冻结产品与验收工具。A10 的唯一 attempt 失败并封存后，用户显式授权 F20/A20；A20 的唯一 attempt 又因 release-owned ClusterRole 遗留失败并封存。用户在 A20 seal 后显式授权一个新的 F30 remediation/freeze cycle，且只允许一次 replacement A30 Clean Acceptance Run：

- Product Contract 只负责部署后真实领域行为和 durable public facts，不感知 acceptance gate 或 promotion。
- Acceptance Runner Contract 只负责唯一 gate DAG、单 gate 编排、durable intent、公开事实 reconciliation、bounded/redacted evidence 和中断恢复。
- Promotion Contract 只依据 immutable identities、完整连续 DAG、artifact hash、HITL attestation 和发布负责人签名形成 Promotion Decision。

Acceptance Evidence Module 是唯一 gate DAG owner。Acceptance Runner 每次命令最多推进一个 frontier；任何外部 effect 前先写 durable intent 并绑定 operation identity。进程中断只允许 reconciliation 同一个 open gate，不能重放 effect 或新增 attempt。任一 mandatory gate failed 后，Clean Acceptance Run 立即 ineligible，后续排障只能进入独立 Diagnostic Evidence Bundle。

所有 User mutation 必须经过真实 Console UI。Acceptance Runner 可在严格 Credential Source/tmpfs 边界内使用动态 Console、Model 和 Notification credential，以无头浏览器自动导航和填写；在 Notification receipt、Connector credential handling、exact diff、Approval、Report publication、manifest review 和 Promotion Decision 等 HITL 点暂停，由真实 User 检查 bounded/no-secret evidence 并签署后才继续。

F10 在无真实 Cluster/provider 的情况下完成所有产品修复、Gate Module、完整 DAG simulation、定向测试、静态检查和 fixed-point code review，并同时冻结 Pilot Release Bundle 与 acceptance-tool artifact。A10/A20 均不做 rehearsal；P03 是 exact Cluster 的唯一自动 baseline check。每个 clean run 从 P01 到 C03 的 gate 都只允许一个 terminal attempt，随后按 `evaluate -> decide -> seal` 完成 eligibility、人工 Promotion Decision 和最终 checksum。A10 与 A20 的 P03 failure 都不得 retry；F30 必须重新执行 offline admission、fixed-point review 和 artifact freeze，A30 使用全新 ledger/tool admission/run-scoped store 重走同一 canonical gate DAG。A30 仍是单 terminal attempt per gate，失败后不得授权 A40 或任何后续 replacement run。

Canonical gate DAG 为：

```text
P01 -> P02 -> P03 -> I01 -> I02 -> I03 -> I04 -> I05
 -> S01 -> S02 -> S03 -> S04 -> S05 -> S06
 -> V01 -> V02 -> V03 -> V04 -> R05 -> V05 -> V06 -> V07
 -> R01 -> R02 -> R03 -> R04 -> R06
 -> V08 -> C01 -> C02 -> C03
 -> evaluate -> decide -> seal
```

`I04` 仅在 `http_nodeport` profile 下允许 `not_applicable`；其他 mandatory gate 必须 passed。

## User Stories

1. As a Platform Operator, I want one checksummed Pilot Release Bundle and one checksummed acceptance-tool artifact, so that product code and acceptance semantics cannot change independently during the run.
2. As a Platform Operator, I want the Clean Acceptance Run to bind exact candidate, tool, DAG, Cluster and access-profile identities, so that all evidence refers to one immutable evaluation target.
3. As a Platform Operator, I want the Acceptance Runner to advance one gate per invocation, so that long PTY sessions cannot silently repeat completed work.
4. As a Platform Operator, I want `status` to verify the ledger and show the only legal frontier, so that I never infer the next action from stale process output.
5. As a Platform Operator, I want an interrupted open gate to resume by reconciliation, so that a process crash does not require replaying an external effect.
6. As a Platform Operator, I want an unprovable interrupted effect to fail the run, so that uncertainty cannot be relabeled as success.
7. As a Platform Operator, I want fixed structured recovery actions rather than arbitrary shell or kubectl input, so that recovery evidence has a bounded, auditable meaning.
8. As a Platform Operator, I want bootstrap admin credentials read from the exact Kubernetes Secret, so that each deployment's dynamic password works without repeated manual entry.
9. As a Platform Operator, I want acceptance-created User credentials kept only in a run-scoped tmpfs store, so that separate gate processes can log in without writing passwords to ordinary disk or evidence.
10. As a Platform Administrator, I want Model and Notification secrets supplied once through a protected input source, so that headless Console automation does not require repeated secret entry.
11. As a Platform Administrator, I want all setup mutations to pass through the real Console UI, so that acceptance proves the actual Web Setup product path rather than a hidden direct API script.
12. As a Platform Administrator, I want invalid Model configuration to fail through the real Provider path before the exact real revision becomes ready, so that readiness cannot be faked by a local check.
13. As a Platform Administrator, I want invalid Notification delivery to dead-letter and the corrected exact revision to send a real message, so that provider false-success cannot pass S04.
14. As a Platform Administrator, I want to confirm the exact Notification Delivery and Destination revision I received, so that later resolved Notifications can reuse that verified revision without another receipt prompt.
15. As a Platform Operator, I want Connector Enrollment credential handling verified once through the Console and rollout path, so that the credential is not retrievable and the exact Cluster becomes ready only after live read verification.
16. As an SRE, I want a real alert webhook request correlated to one Alert Signal and Incident, so that V02 proves causality rather than label similarity.
17. As an SRE, I want the Workbench to expose the accepted Diagnosis Job's frozen Model revision, so that V03 cannot substitute the currently configured revision for historical execution.
18. As an SRE, I want Diagnosis Kubernetes evidence to flow through Gateway authorization and Connector Command, so that the model never receives a hidden direct Cluster path.
19. As an SRE, I want fresh Prometheus, Loki and Kubernetes Evidence Steps tied to the exact verification target, so that Human Input or stale evidence cannot satisfy the Evidence Gate.
20. As an SRE, I want an expired unapproved dry-run Phase to be explicitly retryable through its owner Interface, so that V04 can recover without private Approval SQL or a second Change Request.
21. As an SRE, I want each replacement plan revision to have a distinct awaiting-approval Notification identity, so that an older revision cannot suppress a new review request.
22. As a User without Approval Authority, I want exact diff access, Approval and Execution Grant issuance to fail closed at R05, so that unauthorized access is proved before V05 mutation.
23. As an authorized SRE, I want to inspect the exact target, diff, post-check and rollback status before signing V05, so that the subsequent Console Approval authorizes only the frozen Kubernetes Change.
24. As an authorized SRE, I want the Connector to execute the approved change once and expose a typed terminal post-check result, so that the Acceptance Runner never parses an undeclared private field or retries mutation.
25. As an SRE, I want resolved webhook identity, real recovery metric/log and the full 300-second stabilization interval correlated, so that Incident resolution cannot occur early or by manual override.
26. As an SRE, I want Report v1 assembled from frozen facts and published immutably through the Console, so that V07 preserves the actual Incident outcome.
27. As an SRE, I want the resolved Notification Request, Delivery, provider identity and S04 Destination revision publicly correlated, so that a `sent` label without durable request/provider evidence cannot pass.
28. As a Platform Operator, I want R01-R04/R06 executed in a fixed linear order after V07, so that each disruption and recovery has one attributable cause.
29. As a Platform Operator, I want Connector and Loki unavailability to block only the dependent evidence/change paths and recover without database patching, so that degradation is truthful and bounded.
30. As an SRE, I want R06 resourceVersion drift to produce Stale Change with zero approved mutation and zero automatic retry, so that out-of-band change cannot be hidden or rebased.
31. As an SRE, I want V08 to reopen the same Incident with a new run identity and entirely new Investigation/Evidence/Plan/Approval/Grant/Command history, so that the second round proves restored system capability.
32. As an SRE, I want Report v2 and its resolved Delivery to be independent while Report v1 remains byte-for-byte unchanged, so that rerun history is immutable.
33. As a Platform Operator, I want cleanup to remove only the verification fixture and leave product/governance history readable through public projections, so that cleanup cannot erase the acceptance claim.
34. As a release owner, I want technical eligibility calculated separately from my Promotion Decision, so that tooling cannot automatically authorize a release.
35. As a release owner, I want an ineligible run to accept only a signed `no_promote` decision, so that a human cannot override missing or failed gates.
36. As a release owner, I want the final seal to cover manifest, eligibility, attestations and Promotion Decision, so that no evidence can be added or resigned afterward.
37. As a maintainer, I want old format v1 evidence left untouched and unsupported by the new promotion path, so that legacy retry semantics do not require compatibility code.
38. As a maintainer, I want each Product Module to expose only its own durable facts rather than an acceptance-specific aggregate endpoint, so that test tooling does not become a second product owner.
39. As a maintainer, I want all gate ordering to live in one Acceptance Evidence Interface, so that scripts and Gate Modules cannot drift into contradictory sequences.
40. As a maintainer, I want a complete offline DAG simulation before each candidate freeze, so that no missing R/V/C runner is discovered during its authorized live run.
41. As a maintainer, I want F10 admission tests and review frozen into the acceptance-tool artifact, so that A10/P02 can verify them without rerunning the repository suite in the live window.
42. As an auditor, I want every evidence artifact bounded, normalized, redacted and hash verified, so that secret or raw-provider leakage cannot become part of the promotion record.
43. As an auditor, I want Console operation identities captured from the browser and recoverable from unique public audit/projection facts, so that manual record selection cannot manufacture correlation.
44. As an auditor, I want a failed run's later diagnostics stored separately, so that troubleshooting can continue without changing promotion eligibility.

## Implementation Decisions

- The Product Contract, Acceptance Runner Contract and Promotion Contract are separate. Product Modules do not read the acceptance manifest or gate DAG; Acceptance Runner does not own domain state; Promotion Decision does not authorize Kubernetes mutation.
- Acceptance Evidence Module is the sole owner of the complete DAG, open-gate execution journal, artifact index, identity verification, derived run status, eligibility evaluation and sealing invariants.
- Acceptance Evidence Interface exposes frontier/status verification, atomic gate begin, pre-effect operation binding, terminal result recording, artifact hash verification, evaluate and seal. Promotion decision signing remains a distinct release-owner action.
- Run status is derived from immutable facts rather than stored as a second state machine: open gate means active/open; no open gate with remaining frontier means active/ready; any mandatory failure means ineligible; successful evaluation means eligible; signed decision plus checksum means sealed.
- Each gate has at most one terminal attempt. Any mandatory failure terminalizes the Clean Acceptance Run and forbids all later gates. Diagnostic work creates a separate Diagnostic Evidence Bundle linked to the failed run.
- Process continuity is not required. Safe resume requires a durable pre-effect operation identity and public terminal facts proving exactly-once outcome. Unprovable deadline or outcome becomes failed; mutation is never replayed.
- Existing P/I/S Gate Modules remain intact unless this spec directly changes their contract. New deep Modules are First Run (`V01-V07` plus R05), Recovery (`R01-R04/R06`) and Rerun and Cleanup (`V08/C01-C03`).
- Conductor is a thin composition/dispatch Module. It advances one current frontier per invocation and does not own a second sequence, generic plugin Interface, factory or long-running `run-all` mode.
- CLI supports read-only status, one-gate advance, open-gate resume and the final evaluate/decide/seal sequence. Resume never begins the next gate.
- Artifact phase directories are presentation/storage mapping only. They do not imply ordering; R05 remains in its existing recovery artifact phase while its dependency is `V04 -> R05 -> V05`.
- Recovery ordering is strictly `V07 -> R01 -> R02 -> R03 -> R04 -> R06 -> V08`; these Cluster mutations are never parallelized.
- Clean Acceptance identity freezes Pilot Release Bundle SHA256, acceptance-tool artifact SHA256, evidence format/gate contract revision, Cluster identity and access profile. Any change during the run makes it ineligible.
- The new evidence format does not open, migrate or infer old format v1 bundles. Legacy open attempts return `unsupported_evidence_format`; old bundles remain immutable diagnostics.
- Acceptance Runner monotonic time owns polling/deadline budgets. Product-owned UTC timestamps prove causal domain ordering. A timed gate interrupted without provable original-deadline completion fails rather than restarting its clock.
- P03 checks node/control-plane clock skew as part of the one exact Cluster baseline. There is no automated acceptance rehearsal or duplicate preflight before A10, A20 or the authorized replacement A30.
- Product Modules extend existing actor-scoped projections with stable request/object/revision identities, timestamps and typed outcomes where currently absent. No aggregate acceptance endpoint, acceptance table or acceptance state field is introduced.
- Alertmanager ingress generates or retains a bounded request identity and Incident owner persists it on the corresponding Alert Signal for firing and resolved correlation.
- Workbench projects the exact accepted Diagnosis result's Model revision. Current configuration revision is never used as a historical substitute.
- Diagnosis Kubernetes reads flow only through Gateway-owned authorization and Connector Command. Timeout, failed, truncated and invalid JSON outcomes remain bounded and public.
- Expired unapproved Change Plan Phase retry is decided through the Approval owner's narrow Interface. An approved, started or terminal Phase cannot use this retry path.
- Awaiting-approval Notification identity includes immutable plan revision identity so a later revision cannot collide with an earlier event.
- Connector execution projection exposes the declared terminal post-check result. Acceptance Runner does not parse private stdout conventions.
- Recovery projection exposes resolved webhook identity and the timestamps required to prove Recovery Observation, 300-second stabilization and Incident resolution ordering.
- Notification Delivery projection consistently exposes its Notification Request identity, typed Request, exact Destination revision and provider identity across list, by-event and redelivery reads.
- Browser Adapter drives the real headless Console. It intercepts client-generated request identity before allowing a mutation request to proceed, then binds response object/revision identity. On interruption, resume requires exactly one matching public audit/projection fact.
- Console automation may navigate, fill and submit ordinary steps. S04, S05, V04, V05, V07/V08, C03 and final Promotion Decision pause for bounded/no-secret review and signed HITL confirmation; automation cannot self-attest or self-approve.
- Acceptance Runner may use Console and integration plaintext only through real Credential Source Adapters. Bootstrap password is reread from the exact Kubernetes Secret; acceptance-created User passwords and imported integration secrets live in a run-scoped tmpfs store outside evidence/workspace.
- Credential store directory is mode 0700 and files are mode 0600. Cookies/browser profiles are not persisted. The store is deleted on run failure or seal; credential loss that prevents login fails the run rather than resetting password.
- Integration secret input is a one-time mode-0600 source outside evidence/workspace. Secrets never enter command arguments, environment, PTY, ordinary config, logs, screenshots, traces, audit or evidence. Invalid credentials are derived in memory and not saved.
- Recovery Gate Module accepts only matrix-frozen structured operations against exact targets. It does not accept arbitrary shell or kubectl arguments.
- F10 builds both immutable artifacts only after all owner/contract fixes, admission evidence, complete DAG simulation and fixed-point review are green. Any subsequent source, manifest, image, default or artifact change invalidates the freeze.
- Freeze validity depends only on its immutable inputs: product/runner source, manifest, image digest, default, contract revision, signed admission and artifact bytes. Cluster residue, capacity, clock, registry reachability or other environment outcomes do not by themselves invalidate an unchanged freeze.
- A failed ledger is always evaluated, signed `no_promote` and sealed. If P03 fails during its read-only clean-baseline phase, before any `kubectl apply`, provider call, Notification Delivery or other external effect, the release owner may explicitly authorize a new run under the same verified freeze. Preparation outside the run deletes only the exact release allowlist; the new run uses a new acceptance ID, ledger and tmpfs store, reruns P01/P02 identity/self-check, obtains a new exact-candidate P03 attestation and never reuses an old gate attempt.
- Same-freeze authorization is forbidden when any freeze input changed or the failed run reached an apply/provider/delivery effect. Those cases require a new remediation/freeze cycle before another clean run.
- P02 no longer reruns the full repository checks during A10. It verifies the signed/hashed F10 admission report matches both immutable artifacts and runs only a small acceptance-tool self-check. Fake-backed admission remains explicitly non-live evidence.
- Final stage is three-step and irreversible: evaluate writes eligible/ineligible reasons; release owner signs promote/no_promote, with promote forbidden when ineligible; seal verifies consistency and writes final SHA256SUMS excluding itself. Seal makes the ledger permanently read-only.
- The same natural person may perform several Pilot roles, but each attestation is signed under the role and exact action actually performed.
- Existing uncommitted workspace work is preserved and audited against this spec. It is not automatically accepted, reset or wrapped in compatibility code.

## Testing Decisions

- Tests assert externally observable behavior through Module Interfaces rather than private call order or SQL. Existing high seams are preferred; new seams are limited to Acceptance Evidence, First Run, Recovery, Rerun and Cleanup, Credential Source and Browser Adapter.
- Acceptance Evidence Module tests cover the complete canonical DAG, R05 placement, linear recovery order, one open gate, one terminal attempt, derived status, conditional I04, failure terminalization, identity drift, artifact tamper, old-format rejection and permanent seal.
- Durable-intent tests prove operation identity is persisted before Adapter dispatch, resume reconciles without replay, duplicate dispatch is rejected, and unprovable outcome/deadline fails the gate.
- Conductor tests prove one invocation advances at most one frontier, status is read-only, resume cannot advance, and no duplicate sequence exists outside Acceptance Evidence Module.
- First Run Module tests cover V02 telemetry/public deadlines and webhook correlation; V03 frozen Model revision and fresh Evidence; V04 latest valid revision; R05 authorization denial; V05 exact once execution/post-check; V06 stabilization; and V07 Report/Notification/S04 evidence binding.
- Recovery Module tests cover exact structured actions, before/during/after evidence, owner-by-owner restoration, Connector/Loki truthful degradation, R06 stale/no-mutation/no-retry and fixed linear ordering.
- Rerun and Cleanup Module tests cover same Incident/new Investigation, independent second governance chain, immutable Report v1, Report v2/Delivery, fixture-only deletion, unavailable Resource Target and public governance-history reads.
- Product owner tests cover Alert Signal webhook request identity, Workbench accepted Model revision, Diagnosis Connector read outcomes, expired retry eligibility owner Interface, revision-aware Notification identity, typed post-check, recovery timing and Delivery/Request/provider projections.
- Direct HTTP/OpenAPI/Console consumers are tested whenever an owner projection changes. Authorization tests cover Platform Administrator, ordinary User, authorized SRE and no-Authority User without leaking protected diff or secret metadata.
- Browser Adapter tests use headless Console behavior, separate role contexts, request interception, response identity binding, masked screenshots, HITL pause and the rule that automation cannot generate signatures or approve without confirmation.
- Credential Source tests cover Kubernetes bootstrap reread, CSPRNG User password creation, input mode validation, tmpfs/mode requirements, no command/environment/log/evidence exposure, per-gate re-login, deletion on failure/seal and fail-closed credential loss.
- Full DAG contract simulation drives P01-C03 with existing test substitutes/in-memory Adapters. It covers the successful path, every gate failure, interruption/resume, duplicate effect rejection, tamper, invalid signature, ineligible promotion rejection and old evidence format.
- F10 verification order follows project policy: affected owner tests, direct contract consumers, affected workspace static checks, complete DAG simulation, then fixed-point Standards and Spec review. Full-suite execution is not the default substitute for these seams.
- A10, A20 and A30 remain immutable sealed failed acceptances. Local/fake-backed tests, browser mocks and contract simulations are candidate-admission evidence only and never satisfy a live gate.

## Out of Scope

- Executing any code change, test, build, deployment, provider probe, Notification delivery or live acceptance while producing this spec.
- Automatic retry, mutation or reuse of any sealed ledger is forbidden. Same-freeze runs are not retries: they require the narrow pre-effect environment-failure rule, explicit new authorization and entirely new ledger/store identities. No further run is authorized by this spec merely because cleanup later succeeds.
- Migrating or promoting existing format v1 evidence.
- An acceptance-specific product endpoint, product database state, setup completion flag or browser-side product state machine.
- Arbitrary recovery shell commands, free-form kubectl, manual database patching, seeded Incident state, manual webhook or fake provider response.
- HA, cross-version upgrade, downgrade, backup, data-preserving uninstall or production-Cluster acceptance.
- Automatic external release publication or deployment after a signed Promotion Decision.
- New UI component systems, acceptance-only Console screens or a second authentication mechanism.
- Replacing Gateway, Diagnosis, Connector, MCP, Notification Engine or Console process boundaries.

## Further Notes

- This spec supersedes the old A02/A03/A04 execution order, but preserves their records as history. The old run is continued diagnostic/no-promote evidence, not a blocker completion.
- This spec narrows the earlier acceptance matrix in three places: P02 verifies frozen F10 admission evidence instead of rerunning repository checks in the live window; old evidence format receives no compatibility path; finalization is replaced by evaluate, human decision and seal.
- A10, A20 and A30 sealed `failed_no_promote` remain immutable. F30 was executed under the earlier conservative policy and remains a valid historical freeze; the current policy would handle an A20-like pre-apply residue failure with explicit same-freeze new-run authorization instead of another full freeze.
- Routine operator output defaults to `frontier`, product/tool hashes and gate results. Full green build/checksum logs remain in artifacts and are shown only on failure or explicit request.
- No new ADR is required: the decisions refine acceptance-tool ownership and promotion evidence while preserving accepted product/process architecture in the existing Pilot Release, Web Setup, integration readiness and generic Kubernetes Change ADRs.
- The source design discussion is retained in `acceptance-contract-design.md`; implementation tickets must use this spec as their behavior source and the existing domain glossary for canonical terms.
- Work should proceed by the revised dependency frontier only. Every implementation ticket starts in a fresh context and closes with its targeted tests and fixed-point Standards/Spec review before the next dependent ticket starts.
