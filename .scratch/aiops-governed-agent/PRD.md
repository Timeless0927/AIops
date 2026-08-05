Status: ready-for-agent

# AIOps 通用 Chat 与受治理 Agent 循环规格

## Problem Statement

当前 AIOps 已经具备 Incident、Investigation、Diagnosis Job、Evidence Step、Evidence Gate、RBAC、MCP、Approval、可观测性和 Console，但 Agent 能力主要服务于告警触发的结构化 Diagnosis。用户无法从一个通用 Chat 入口询问平台知识、查询自己有权查看的真实环境，并在必要时把对话升级为正式 Investigation。

当前 Diagnosis 工具循环也没有充分结合通用问答 Agent 的优点：工具 Observation 回灌过于摘要化，最终结构不合规时缺少受控修复，调查停止主要依赖模型输出或轮次上限，跨执行重试不能完整恢复每轮进展。已有 Investigation Event、Evidence Step 和 Workbench 能展示调查结果，但 Tool Activity、候选原因、Evidence 关系和停止原因没有形成完整的 Decision Trace，用户难以判断结论为何可信。

直接合并 SxDevOps 或增加另一套 Agent 平台会复制用户、权限、任务、MCP、状态和前端边界，也会绕开 AIOps 已有治理模型。需要在现有边界内增加通用 Chat，并把 SxDevOps 中有效的工具循环、fallback、冲突检查和答案修复行为接入当前受治理的 Investigation 模型。

## Solution

AIOps 增加独立的 Chat Session 产品入口。Chat 可以回答通用知识问题；当问题涉及真实环境时，Gateway 先按当前 User 的 RBAC 冻结资源范围，Diagnosis 才能调用管理员启用的只读 MCP Integration 或 Skill。模型可以缩小查询范围，但不能扩大范围，也不能调用 mutation 工具。

Chat Session 与 Investigation 不共享同一个会话对象。User 可以显式执行 Investigation Handoff，把确认过的聊天内容作为 Human Input 关联到已有 Incident，或在没有 Alert Signal 时创建带真实资源范围的 User-created Incident 和第一轮 Investigation。Handoff 不把聊天内容升级为 Evidence，也不产生 Approval 或执行权。

Diagnosis 内形成一个供 Chat 和 Investigation 两个真实调用方使用的受治理工具循环。知识问答可以不调用工具；涉及真实环境的 Chat 必须取得工具 Observation；正式 Investigation 必须覆盖规定的证据来源或明确记录缺失原因。工具结果以结构化、脱敏、受大小限制的 Observation 回灌模型，包含关键事实、代表性样本、查询范围、截断说明和 Evidence reference，而不是一句摘要或无限原始数据。

模型提出结束后，系统验证必需来源、Evidence 引用、事实冲突、缺失证据和预算。最终输出不合规时允许一次受控修复，仍失败则生成安全的结构化部分结果。每轮模型调用、工具状态、Evidence reference 和剩余预算持久化，使执行可以从最后一个安全边界恢复而不重复已完成调用。

Diagnosis 的“思考、观察、循环”是本规格的首要修复对象。思考不保存模型原始 Chain of Thought，而是维护结构化候选原因、支持/反驳 Evidence、未知项和下一步检查；观察必须把规范化后的实际关键事实回灌下一轮，而不是只回灌步骤 ID 或一句摘要；循环按“调查目标 → 候选原因 → 工具调用 → Observation → 候选更新 → 继续/停止”推进，并由系统校验停止条件、执行一次答案修复和持久化安全 checkpoint。

Console 增加 Chat 工作入口和管理员 MCP/Skill 管理页面。Workbench 复用现有 Investigation Event、Tool Activity 和 Evidence Step，展示结构化 Decision Trace：调查目标、工具目的、脱敏范围、状态、耗时、结果、Evidence、对候选原因的影响以及继续或停止原因。系统不保存或展示原始 Chain of Thought、系统提示词、敏感输入、凭据或未脱敏原始 payload。

## User Stories

1. As a User, I want to open a Chat Session from Console, so that I can ask AIOps questions without first locating an Incident.
2. As a User, I want knowledge questions answered without unnecessary tool calls, so that simple questions remain fast and inexpensive.
3. As a User, I want environment-specific answers to query real tools before making factual claims, so that the model does not guess about live state.
4. As a User, I want to choose a Cluster, namespace, Service, Deployment Target, or Incident scope for Chat, so that tool queries have an understandable boundary.
5. As a User, I want Chat to reject a scope I cannot access, so that knowing a resource name does not grant access.
6. As a User, I want the model to narrow but never widen my authorized scope, so that tool selection cannot bypass RBAC.
7. As a User, I want Chat to remain read-only, so that a conversation cannot mutate Kubernetes.
8. As a User, I want mutation advice to be clearly presented as advice, so that it is not confused with an approved action.
9. As a User, I want my Chat Sessions private by default, so that exploratory questions are not exposed to unrelated users.
10. As a User, I want to reopen recent Chat Sessions, so that I can continue a useful conversation.
11. As a User, I want ordinary Chat messages removed after the fixed retention period, so that exploratory content is not retained indefinitely.
12. As a User, I want to select which Chat messages are transferred during Investigation Handoff, so that irrelevant conversation does not enter an Incident.
13. As a User, I want Handoff to an existing Incident when one already represents the problem, so that operational history is not fragmented.
14. As a User, I want to create a User-created Incident from Chat when no alert exists, so that an unalerted operational problem can receive a governed Investigation.
15. As a User, I want a User-created Incident to require a real resource scope and problem summary, so that Chat cannot create unbounded operational cases.
16. As a User, I want transferred Chat context recorded as Human Input, so that its origin and non-evidence status remain visible.
17. As a User, I want Handoff to require an explicit action, so that Chat never silently creates an Incident.
18. As an SRE, I want formal Investigation to query required evidence sources or record why they are unavailable, so that missing coverage is visible.
19. As an SRE, I want each tool call to state what it is checking and why, so that I can follow the Investigation.
20. As an SRE, I want tool results to include key facts and representative redacted samples, so that Observation remains useful without copying entire data sets.
21. As an SRE, I want query scope, time range, truncation, redaction, and failures recorded, so that I understand the limits of each result.
22. As an SRE, I want a tool failure to leave a visible partial result and allow other independent sources to continue, so that one unavailable backend does not erase useful work.
23. As an SRE, I want repeated or already completed tool calls avoided after process recovery, so that retries do not waste capacity or create inconsistent observations.
24. As an SRE, I want an Investigation to stop when evidence is sufficient or its fixed budget is exhausted, so that it neither stops prematurely nor loops forever.
25. As an SRE, I want budget exhaustion to produce a partial or needs-human result, so that uncertainty is explicit.
26. As an SRE, I want conclusions checked against tool facts, so that the answer cannot claim that logs are empty when the query returned errors.
27. As an SRE, I want an invalid final model response repaired once, so that a recoverable formatting error does not fail an otherwise useful Investigation.
28. As an SRE, I want a safe system-generated result after repair fails, so that malformed model output is never exposed as a confident diagnosis.
29. As an SRE, I want every environment-specific conclusion to cite Evidence, so that I can verify its basis.
30. As an SRE, I want confidence shown only as supplementary information, so that it cannot substitute for Evidence Gate.
31. As an SRE, I want Decision Trace to show which result supports or refutes each candidate cause, so that competing explanations remain visible.
32. As an SRE, I want the stopping reason displayed, so that I know whether the Investigation ended from sufficient evidence, missing evidence, failure, or budget.
33. As an SRE, I want raw Chain of Thought excluded, so that model-private drafts are not mistaken for facts or exposed as sensitive data.
34. As an SRE, I want secrets, Secure Input, system prompts, credentials, and unredacted payloads excluded from Chat and Decision Trace, so that explainability does not weaken security.
35. As a Platform Administrator, I want to register and verify an MCP Integration, so that available tools are known before users can invoke them.
36. As a Platform Administrator, I want to enable or disable an MCP Integration and inspect its health, so that an unsafe or unavailable provider can be removed from use.
37. As a Platform Administrator, I want to review a snapshotted tool catalog and allowed scope, so that a server cannot silently expand usable capabilities.
38. As a Platform Administrator, I want AIOps policy to classify tools as allowed read-only capabilities, so that a server's self-description is not treated as authorization.
39. As a Platform Administrator, I want MCP credentials stored and displayed through existing secure administration rules, so that secrets do not enter Console state or audit payloads.
40. As a Platform Administrator, I want MCP registration, verification, enablement, and use audited, so that capability changes are attributable.
41. As a Platform Administrator, I want to create and version a Skill containing instructions or workflow guidance, so that repeated operational practices can be reused.
42. As a Platform Administrator, I want a Skill to declare required MCP tools and applicable scope, so that invalid dependencies are visible before enablement.
43. As a Platform Administrator, I want to enable or disable a Skill without deleting its history, so that active behavior remains attributable to a version.
44. As a Platform Administrator, I want Skills prevented from running arbitrary code or expanding User authority, so that content management cannot become remote execution.
45. As a Console user, I want Chat history, selected scope, tool progress, citations, and Handoff controls in one work-focused view, so that the workflow does not require hidden navigation.
46. As a Console user, I want Workbench Tool Activity expandable without showing raw JSON by default, so that investigation progress remains readable.
47. As a Console user, I want failed, skipped, truncated, and redacted states labeled clearly, so that an empty-looking panel is not mistaken for a successful check.
48. As a Console user, I want Chat and Investigation updates to resume after reconnecting, so that browser refresh does not lose accepted progress.
49. As a platform operator, I want bounded metrics for Chat requests, tool outcomes, repair outcomes, budget exhaustion, and recovery, so that Agent reliability is observable without high-cardinality labels.
50. As an auditor, I want Chat Handoff, MCP/Skill administration, tool authorization denial, and Investigation Decision Trace linked to actor and request identity, so that governance can be reconstructed.

## Implementation Decisions

### Product Model

- Chat Session and Investigation remain separate domain objects with separate lifecycle and visibility rules.
- Chat Session is private to its creator in the first version. Session sharing, branching, side threads, and collaborative editing are excluded.
- Investigation Handoff is an explicit User command. It copies only User-selected context as Human Input and never promotes Chat content to Evidence, Approval, Recommended Action, or execution authority.
- Handoff may attach to an existing Incident or create a User-created Incident with a validated resource scope and initial Investigation.
- User-created Incident follows the same Investigation, Evidence Gate, Approval, resolution, report, and audit rules as an alert-originated Incident. It may have no Alert Signal.
- Chat Session content uses a fixed 30-day retention period. Human Input copied into an Incident follows the existing governance retention rules.

### Process And State Ownership

- Existing Console, Gateway, Diagnosis, MCP, Connector, and Notification Engine process boundaries remain unchanged. No Chat Agent service is added.
- Browser static assets are served by Console through the shared origin; browser API, authentication, and event-stream traffic goes through Gateway. Browser code never connects directly to Diagnosis, MCP, Connector, or observability backends.
- Gateway owns Chat Session identity, messages, RBAC-derived scope, Handoff, audit, retention, and public API/event contracts.
- Diagnosis owns model execution and the shared governed tool loop for Chat and Investigation requests.
- MCP Integration owns no AIOps product state. It returns observations through a declared capability boundary.
- The shared loop becomes an internal Module only when Chat is introduced as the second real caller. The work does not create a generic Agent platform, provider plugin hierarchy, or parallel implementation.

### Tool Authorization And Scope

- General knowledge Chat may answer without a tool. Any claim about live environment state requires at least one successful authorized tool Observation or an explicit unavailable/partial result.
- Gateway resolves and freezes the User's allowed resource scope for each environment-specific request. Model output is never an authorization input.
- Diagnosis and MCP calls carry the frozen scope. The model may narrow a query but cannot widen Cluster, namespace, Service, Deployment Target, Incident, or time constraints beyond policy.
- Chat may call only platform-enabled query capabilities classified by AIOps policy as read-only. Mutation-like, unknown, changed, or unverified tools fail closed.
- No Chat message, Skill, MCP response, or model output can create Approval, Execution Grant, or Connector mutation command.

### Governed Tool Loop

- Diagnosis 显式维护结构化 Hypothesis State：候选原因、支持 Evidence、反驳 Evidence、未知项和下一步检查。该状态是可审计的决策数据，不是模型原始 Chain of Thought。
- 每轮按“调查目标 → 候选原因 → 工具调用 → Observation → 候选更新 → 继续/停止”推进；下一轮模型必须取得规范化 Observation 的实际关键事实，而不只是 Evidence Step 标识或一句摘要。
- The loop supports distinct policy profiles rather than separate implementations: knowledge Chat may use no tool, environment Chat requires a relevant tool, and Investigation requires its evidence checklist or an explicit missing-source result.
- Tool Observation passed to the model contains a structured summary, key facts, representative redacted samples, scope, time range, outcome, truncation/redaction metadata, and Evidence reference.
- Full raw logs, metric series, Kubernetes objects, credentials, and unbounded payloads are not copied into model context or Gateway state. Raw data remains in its owner backend and is accessed through references and follow-up queries.
- Each request has fixed service-owned limits for model turns, tool calls, per-result size, total context, and wall-clock duration. Ordinary Users cannot modify those limits in the first version.
- Each model turn, proposed tool call, normalized tool result, Evidence reference, remaining budget, and completion state is durably checkpointed at a safe boundary.
- Recovery reuses completed normalized observations and never silently repeats a completed tool call. An uncertain in-flight call is reconciled or surfaced rather than assumed successful.
- Independent tool-source failures do not abort remaining useful reads. Missing required sources constrain the result to partial or needs-human.

### Completion And Answer Repair

- The model proposes a final answer; deterministic policy decides whether the request may complete.
- Investigation completion checks required-source coverage, Evidence reference integrity, current scope, contradiction with normalized tool facts, declared missing evidence, and remaining budget.
- Environment Chat uses the same contradiction and citation checks but does not require the full Investigation evidence checklist.
- An invalid final contract or repairable contradiction receives one bounded model repair attempt using the validation findings. A second failure returns a deterministic safe result assembled from accepted observations.
- Environment answers contain conclusion, checked sources, Evidence references, uncertainty, and next step. Knowledge answers use normal conversational form.
- Model confidence is optional presentation metadata and never satisfies Evidence Gate or authorizes a mutation.

### Decision Trace

- Decision Trace is assembled from accepted model goals, normalized Tool Activity, Evidence Steps, candidate-cause relationships, validation outcomes, and the completion reason.
- User-visible Tool Activity includes purpose, safe parameters or scope, status, duration, result summary, Evidence references, candidate-cause impact, and continuation or stopping reason.
- Existing durable Investigation Event and Evidence Step contracts are extended or reused as the visible source of truth. A second trace store and raw model transcript are not introduced.
- Raw Chain of Thought, system prompts, hidden model messages, Secure Input, credentials, unredacted logs, and arbitrary payloads are neither persisted nor displayed.

### MCP And Skill Administration

- Platform administration provides MCP Integration list, registration, verification, enablement, health, capability snapshot, allowed scope, credential update, and audit history.
- A newly discovered or changed tool is unavailable until AIOps policy explicitly allows its read-only capability and scope.
- Skill administration provides versioned instruction/workflow content, enablement, applicable scope, and required MCP tool references.
- A Skill contains no executable code, uploaded plugin bundle, dependency installation, browser script, or authority grant.
- Disabling an MCP Integration or Skill prevents new use without rewriting historical Chat or Investigation records.

### Console And API

- Console adds a first-level Chat route alongside the Incident workflow and adds MCP/Skill administration under the permission-gated admin surface.
- Chat view combines session history, resource scope, message input, Tool Activity, citations, uncertainty, and explicit Investigation Handoff.
- Workbench expands Tool Activity and diagnosis output into Decision Trace while keeping Evidence Step as the durable evidence presentation unit.
- Default views render readable fields, not raw JSON, Python dictionaries, Kubernetes object dumps, or hidden reasoning.
- Gateway remains the only public business API and event-stream contract. Console server state continues to come from Gateway, with URL state for navigation and local state only for transient interaction.

### Delivery Order

- First improve the existing Investigation loop and Decision Trace using current contracts.
- Introduce the shared internal loop only when the Chat vertical slice creates the second real caller.
- Deliver Chat and explicit Handoff before dynamic MCP/Skill administration, initially using the already configured read-only MCP capabilities.
- Add MCP Integration and Skill administration after the authorization and loop contracts are stable.
- Reuse SxDevOps behavior as reference for Observation feedback, fallback, conflict checking, and answer repair. Do not merge repositories or adopt its static multi-Agent orchestration preview.

## Testing Decisions

- Tests verify externally observable behavior through public Module or HTTP/SSE contracts, not private prompt text, helper call order, model wording, database table layout, or React component internals.
- The primary acceptance seam is a deterministic Gateway HTTP/SSE flow with fake model and fake MCP: Chat, authorized read-only tool, cited answer, explicit Handoff, Investigation, Evidence Step, and Decision Trace.
- Authorization tests prove that unknown resources, unauthorized scope expansion, disabled tools, changed capability snapshots, mutation-like tools, and Skill-requested authority escalation fail closed.
- Diagnosis contract tests prove knowledge Chat may skip tools, environment Chat cannot claim live facts without tools, Investigation records missing required sources, Observation is structured and bounded, contradictions are rejected, one repair is attempted, and repeated failure returns a safe result.
- Recovery tests close and reopen durable owners after accepted model/tool checkpoints and prove completed calls are not repeated, in-flight ambiguity is visible, and remaining work resumes once.
- Handoff tests prove selected messages become Human Input, omitted messages do not transfer, content is not Evidence, an existing Incident can be selected, and a scoped User-created Incident can be created without an Alert Signal.
- Decision Trace contract tests prove Tool Activity contains safe purpose/scope/outcome/Evidence/impact/stopping fields and excludes Chain of Thought, prompts, credentials, Secure Input, unredacted samples, and raw payloads.
- MCP/Skill administration tests cover fresh authentication where required, credential masking, capability verification, explicit enablement, version history, dependency validation, audit, and disabled-state behavior.
- Console tests use a fixed Gateway contract to verify Chat states, scope errors, tool progress, citations, Handoff confirmation, expandable Decision Trace, and MCP/Skill administration states. TypeScript no-emit and production build remain required.
- Daily ticket verification uses affected Module tests and direct contract consumers. Real model, external MCP, full browser matrix, and Kubernetes-level acceptance are reserved for explicit release validation.

## Out of Scope

- Raw Chain of Thought, system-prompt display, model debug transcripts, or unrestricted raw evidence exploration.
- A real multi-Agent planner, parallel specialist Agents, Agent-to-Agent delegation, or adoption of SxDevOps orchestration-preview records.
- A new Chat Agent process, browser-to-MCP connection, browser-to-Diagnosis connection, or a second public API owner.
- Chat-initiated Kubernetes mutation, automatic Approval, natural-language execution confirmation, or bypass of Evidence Gate and Execution Grant.
- Arbitrary-code Skills, uploaded executable packages, dependency installation, plugin marketplace, community Skill distribution, or server-side script execution.
- User-configurable model/tool budgets, advanced Chat sharing, branching, side threads, organization-wide Chat search, or indefinite Chat retention.
- Copying full logs, metrics, traces, Kubernetes objects, or MCP payloads into Gateway databases.
- Repository merge with SxDevOps or migration of its user, RBAC, task, observability, or frontend implementations.
- Real external model/MCP acceptance, complete browser matrix, and real Kubernetes mutation as ordinary ticket-level tests.

## Further Notes

- The existing V1 control-plane and Console model is the baseline, not a legacy system to replace. This work is an incremental capability after the completed V1 contract.
- Existing Investigation Event, Tool Activity, Evidence Step, Evidence Gate, Diagnosis Job, Workbench, RBAC, Approval, and MCP process boundaries should be reused before adding new abstractions.
- SxDevOps demonstrates a useful single-Agent tool loop with complete tool-message feedback, fallback, answer conflict checks, and formatter repair. Its multi-Agent Plan+ReAct records are static previews and are not an implementation target.
- The minimum useful first release is an improved governed Investigation loop plus visible Decision Trace. Chat is the second real caller that justifies extracting the common loop; MCP/Skill administration follows after that behavior is proven.
