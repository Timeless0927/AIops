Status: wontfix

Superseded by `../aiops-v1-model/PRD.md`. Keep this file only as historical design input; do not implement it.

# AIOps Console Incident Workbench Redesign PRD

## Problem Statement

旧版 AIOps Console 把太多未成型能力做成了一等页面：Agent Runs、Runbooks、KB 候选、审批中心、审计、集群、用户、设置、搜索等入口并列出现，但核心事件工作台反而不可用。

用户面对事件详情时看不到真正有用的信息：标题过大、信息密度低、详情像乱码、过程图不可读、脱敏片段被截断、Agent Chat 发消息后看不到回复、开始调查按钮可重复点击却只是刷新当前 Run。集群和用户模型也不可信：集群可被手动乱填，用户权限靠手填团队/服务/集群/命名空间字符串，和真实 Connector、服务归属、团队绑定脱节。

这次重做的目标不是继续堆功能，而是把第一版 Console 收敛成一个事件优先、证据可读、Agent 反馈可信、资源绑定合理的 Incident Workbench。

## Solution

第一版 Console 只围绕事件工作台交付。

保留的产品路由只有：

- `/login`
- `/incidents`
- `/incidents/:incidentId`
- `/incidents/:incidentId/report`

事件详情页使用紧凑状态条和三栏工作台：

- 左栏：事件事实、资源上下文、时间线、人工备注
- 中栏：调查过程、Evidence Steps、Agent 交互
- 右栏：当前判断、建议动作、需要确认、责任摘要

UI 不再暴露独立 Agent Runs。后端可以继续使用 run/run event 存储，但前端只展示事件内“调查线程”。发送消息后，用户消息、等待状态、Agent 回复、工具调用和证据更新必须立即可见。

证据展示单位改成 Evidence Step：每一步说明查什么、为什么查、结果是什么、对当前判断有什么影响。脱敏样本结构化展示，不再把原始数据截成不可读片段。建议动作必须引用支撑它的 Evidence Step。

资源模型改为绑定优先：集群只能由 Connector 注册产生，管理员只能编辑已注册集群的显示名、环境、归属和策略；服务、团队、部署目标来自发现、CMDB 或告警线索再由人确认；用户权限通过 User -> Team -> Service -> DeploymentTarget 链路解析，不再靠用户表里的自由文本 scope。

## User Stories

1. As an SRE, I want the Console to open on incidents, so that I can start from active operational work instead of a generic dashboard.
2. As an SRE, I want the incident list to show unresolved incidents clearly, so that I can pick the next event to investigate.
3. As an SRE, I want an incident detail page with a compact status bar, so that the first screen is not consumed by oversized titles.
4. As an SRE, I want the incident status bar to show severity, title, state, impact, time range, resource identity, owner, connector state, and report action, so that I can orient in seconds.
5. As an SRE, I want event facts grouped in the left column, so that status, severity, start time, duration, source, and fingerprint are easy to scan.
6. As an SRE, I want resource context grouped in the left column, so that cluster, namespace, service, team, Connector status, and binding status are visible together.
7. As an SRE, I want a clear event timeline, so that alert trigger, investigation start, evidence updates, human notes, and status changes are traceable.
8. As an SRE, I want to add human notes to an incident, so that facts outside the Agent can be preserved.
9. As an SRE, I want the investigation process to be the center of the page, so that I can see how the conclusion was reached.
10. As an SRE, I want evidence steps above chat, so that I review operational facts before reading conversation.
11. As an SRE, I want each investigation step to show what was checked, so that I know what work the Agent performed.
12. As an SRE, I want each investigation step to show why it was checked, so that the investigation path is understandable.
13. As an SRE, I want each investigation step to show its data source, so that I know whether the evidence came from metrics, logs, traces, K8s, topology, change data, or a tool.
14. As an SRE, I want each investigation step to show a human-readable result, so that I do not have to parse JSON to understand the finding.
15. As an SRE, I want each investigation step to show whether it supports, refutes, or leaves uncertain the current hypothesis, so that I can evaluate confidence.
16. As an SRE, I want to expand an evidence step to see redacted samples, so that I can verify important details without seeing secrets.
17. As an SRE, I want redacted samples to preserve operational context, so that service name, error type, status code, trace id, duration, and resource identity remain useful.
18. As an SRE, I want redacted fields to explain why they are hidden, so that `[已脱敏: token]` is understandable.
19. As an SRE, I want the UI to avoid raw JSON by default, so that the workbench does not feel like a data dump.
20. As an SRE, I want failed, skipped, and empty evidence states to explain why, so that I can decide the next step.
21. As an SRE, I want the right column to show the current judgment first, so that root cause, confidence, impact, and unknowns are always visible.
22. As an SRE, I want suggested actions to show target, risk, reason, pre-check, and confirmation requirement, so that I can assess them quickly.
23. As an SRE, I want suggested actions to cite evidence steps, so that I can see why the action is recommended.
24. As an SRE, I want uncertain evidence to force human confirmation on actions, so that weak evidence does not produce overconfident remediation.
25. As an SRE, I want missing or failed key evidence to produce next-check recommendations instead of confident remediation, so that the system does not guess.
26. As an SRE, I want required human confirmations grouped in the right column, so that blocking questions are not buried in chat.
27. As an SRE, I want a responsibility summary, so that requester, Agent, confirmer, executor, and audit state are visible without raw payloads.
28. As an SRE, I want the Agent investigation represented as an incident-scoped thread, so that I do not have to understand standalone Agent Runs.
29. As an SRE, I want only one active main investigation thread per incident, so that repeated clicks do not create duplicate runs.
30. As an SRE, I want "开始调查" to change immediately to "调查中", so that I know the click took effect.
31. As an SRE, I want the start button disabled while investigation is running, so that I cannot accidentally restart the same investigation.
32. As an SRE, I want to continue asking questions during a running investigation, so that I can guide the Agent with new context.
33. As an SRE, I want my message to appear immediately after sending, so that I know it was accepted.
34. As an SRE, I want to see "Agent 正在处理你的问题" after I send a message, so that the interface does not look dead.
35. As an SRE, I want streamed Agent replies to appear progressively, so that I can follow long responses.
36. As an SRE, I want tool call start and finish events to appear in the investigation process, so that I know what the Agent is doing.
37. As an SRE, I want evidence updates to appear as steps, so that tool results become readable investigation facts.
38. As an SRE, I want a visible waiting state if no Agent event arrives within 10 seconds, so that silence is explained.
39. As an SRE, I want retry options on failed messages, so that transient failures are recoverable.
40. As an SRE, I want to pause an investigation, so that the Agent stops progressing temporarily.
41. As an SRE, I want to terminate an investigation, so that an unhelpful or stale investigation can be stopped.
42. As an SRE, I want to mark human takeover, so that the event records that a human is now leading.
43. As an SRE, I want human takeover to stop Agent automatic progression, so that the Agent does not keep generating new suggestions.
44. As an SRE, I want human takeover to preserve evidence and messages, so that context is not lost.
45. As an SRE, I want to restart investigation after finished or terminated states, so that a new round is explicit and traceable.
46. As an SRE, I want finished investigations not to restart implicitly when I type, so that the system avoids hidden magic.
47. As an SRE, I want reports generated from incidents, so that I can share known facts, timeline, evidence, conclusion, notes, and follow-up items.
48. As an SRE, I want unknowns to remain unknown in reports, so that reports do not invent missing facts.
49. As an SRE, I want reports to cite evidence steps, so that the report is traceable to readable investigation facts.
50. As a platform admin, I want clusters to be created only by Connector registration, so that the Console cannot invent fake clusters.
51. As a platform admin, I want to edit registered cluster display names, so that operators can see human-friendly names.
52. As a platform admin, I want to edit registered cluster environment and owner team, so that risk and routing are explicit.
53. As a platform admin, I want Connector heartbeat to own runtime status, so that online/offline state cannot be faked manually.
54. As a platform admin, I want no "添加集群" flow, so that invalid configuration cannot enter through the UI.
55. As a platform admin, I want services to come from Connector discovery or service catalog records, so that bindings reflect real resources.
56. As a platform admin, I want alert labels treated as temporary clues, so that weak labels do not become permanent truth without confirmation.
57. As a platform admin, I want to confirm or correct discovered service bindings, so that ownership can be fixed without inventing resources.
58. As a platform admin, I want a Service to have multiple DeploymentTargets, so that one service can run in many clusters/namespaces.
59. As a platform admin, I want Teams to own Services, so that permission and routing follow service ownership.
60. As a platform admin, I want users to belong to Teams through role bindings, so that user access is not a pile of hand-entered strings.
61. As a team operator, I want access to incidents for services my team owns, so that I can investigate my own systems.
62. As a team approver, I want confirmation authority scoped to my team-owned services, so that approval is tied to responsibility.
63. As a platform auditor, I want read-only access according to audit policy, so that I can inspect responsibility without mutating systems.
64. As an SRE, I want unbound incidents to remain visible, so that missing ownership does not hide operational problems.
65. As an SRE, I want unbound incidents to disable execution-class actions, so that the system does not act on unknown resources.
66. As an SRE, I want unbound incidents to explain "需要确认资源归属", so that I know what blocks remediation.
67. As an SRE, I want to associate an unbound incident with an existing Service/DeploymentTarget, so that the incident can become actionable.
68. As a mobile user, I want responsive incident awareness, so that I can see status, conclusion summary, investigation steps, and blocking confirmations on a small screen.
69. As a mobile user, I want complex investigation and resource management to stay desktop-first, so that mobile does not become an unusable compressed admin console.
70. As a developer, I want old backend stores reused where appropriate, so that the redesign does not become a full backend rewrite.
71. As a developer, I want incident-level APIs in front of existing run/event internals, so that the frontend stays product-oriented.
72. As a developer, I want acceptance tests at the Workbench API and UI behavior seam, so that product regressions are caught without over-testing internals.

## Implementation Decisions

- The first-version Console route set is limited to `/login`, `/incidents`, `/incidents/:incidentId`, and `/incidents/:incidentId/report`.
- The primary navigation contains only the Incident Workbench path.
- The first version removes these top-level product surfaces: Agent Runs, new Agent Run, Agent Run detail, Runbooks, Approvals, Approval detail, Audit, Audit detail, Policies, Clusters, Users, User detail, Settings, Search, KB candidate approval, and full management pages.
- Agent Runs remain an internal storage concept only. The UI language is "调查线程" or "本次调查".
- Approvals are folded into incident detail as "待确认动作" or "需要确认".
- Audit is folded into incident detail as a responsibility/audit summary.
- Resource state is displayed inside incident context, not as a standalone management area.
- The report page remains as a lightweight child page for preview/export. It has no report versioning, publish flow, or KB candidate generation in this spec.
- The incident detail page uses a compact status bar around 72px high instead of a large title card.
- Desktop layout uses three columns: left `280px`, center `minmax(560px, 1fr)`, right `360px`.
- Narrow screens stack in this order: conclusion summary, investigation process, incident context.
- The left column includes only event facts, resource context, timeline, and human notes.
- The left column must not include raw JSON, full label dumps, all historical runs, or full audit records.
- The center column prioritizes investigation steps before Agent chat.
- The right column order is fixed: current judgment, recommended actions, needs confirmation, responsibility summary.
- Empty/loading/error states are part of the feature contract. The UI must show clear states for no incidents, loading incident, no evidence, Agent running, no Agent reply yet, Connector offline, permission-limited data, and errors.
- The frontend calls incident-level investigation APIs instead of `/api/agent-runs/*`.
- Required incident-level investigation API surface:
  - `GET /api/incidents/:incidentId/workbench`
  - `POST /api/incidents/:incidentId/investigation/start`
  - `POST /api/incidents/:incidentId/investigation/messages`
  - `POST /api/incidents/:incidentId/investigation/pause`
  - `POST /api/incidents/:incidentId/investigation/terminate`
  - `POST /api/incidents/:incidentId/investigation/takeover`
  - `GET /api/incidents/:incidentId/investigation/events`
- Starting investigation returns the existing running thread if one already exists. It must not create duplicate active investigations.
- The investigation SSE endpoint is scoped by incident, so the frontend does not need to know `run_id`.
- Minimum investigation event types:
  - `investigation_started`
  - `investigation_phase_changed`
  - `user_message_added`
  - `agent_message_delta`
  - `agent_message_done`
  - `tool_call_started`
  - `tool_call_finished`
  - `evidence_step_added`
  - `evidence_step_updated`
  - `action_recommended`
  - `human_confirmation_required`
  - `investigation_paused`
  - `investigation_terminated`
  - `human_takeover_started`
  - `investigation_failed`
  - `investigation_finished`
- Investigation state machine:
  - `not_started`: show "开始调查", enabled.
  - `running`: show "调查中", disable start; show pause, terminate, takeover.
  - `paused`: show continue and terminate.
  - `takeover`: show "人工接管中"; Agent automatic progression stops; input becomes human notes/context.
  - `terminated`: show "重新调查"; message input disabled.
  - `finished`: show "重新调查"; no implicit new run when typing.
  - `failed`: show retry and failure details.
- Message input behavior:
  - `running`: enabled and sends to Agent.
  - `paused`: enabled, but sending tells the user to continue investigation first.
  - `takeover`: enabled as human notes only.
  - `terminated`: disabled.
  - `finished`: disabled until the user explicitly starts "重新调查".
- `/btw`, promote, multiple parallel discussion threads, and token/cost as primary UI are out of the first version.
- Evidence presentation unit is `EvidenceStep`, not process node or raw data-source node.
- `EvidenceStep` contract:

```ts
type EvidenceStep = {
  step_id: string
  title: string
  status: 'pending' | 'running' | 'complete' | 'failed' | 'skipped'
  finding: 'supports' | 'refutes' | 'uncertain' | 'not_applicable'
  why: string
  source: 'metrics' | 'logs' | 'traces' | 'k8s' | 'topology' | 'change' | 'tool'
  query_summary: string
  result_summary: string
  impact: string
  time_range: { from: string; to: string }
  scope: {
    cluster: string
    namespace: string
    service: string
    team?: string
  }
  refs: EvidenceRef[]
  samples: RedactedSample[]
  error?: { code: string; message: string; retryable: boolean }
}
```

- `title`, `why`, `result_summary`, and `impact` must be readable Chinese UI copy.
- `samples` may be empty, but `result_summary` must not be empty.
- Failed and skipped evidence steps must explain why.
- The frontend must not default-render arbitrary JSON for evidence steps.
- Redacted sample contract:

```ts
type RedactedSample = {
  sample_id: string
  kind: 'log' | 'metric' | 'trace' | 'k8s_event' | 'change' | 'table'
  summary: string
  fields: Array<{
    label: string
    value: string
    redacted: boolean
    redaction_reason?: 'secret' | 'token' | 'password' | 'personal_data' | 'policy'
  }>
  raw_ref?: string
}
```

- Redacted samples default to summary plus key fields.
- Redacted fields show a reason, for example `[已脱敏: token]`.
- Long logs preserve operational context rather than using raw string truncation as the main presentation.
- Raw references appear as `raw_ref`; raw JSON is not expanded by default.
- Recommended actions must reference at least one `EvidenceStep.step_id` or evidence ref.
- Recommended action cards show 1-3 evidence step titles as "依据".
- If supporting evidence is `uncertain`, the action is marked "需要人工确认".
- If key evidence failed or is missing, the UI shows a next-check recommendation instead of a confident remediation action.
- Incident reports cite evidence steps, not loose evidence ids.
- Cluster identity is created only by Connector registration. The Console has no manual "添加集群" flow.
- Admins may edit only registered cluster business/configuration fields: `display_name`, `environment`, `owner_team`, and policy switches or notes.
- Connector heartbeat/runtime reporting owns online/offline/degraded state, `connector_id`, last heartbeat, and runtime failure summary.
- Admins cannot manually mark a cluster online/offline.
- If no Connector has registered, the Console shows "暂无已注册集群" and provides no free-text cluster creation.
- The old cluster config/runtime split is directionally reusable, but cluster config upsert must not create a new `cluster_id` from arbitrary Console input.
- Service, Team, and DeploymentTarget use discovery plus confirmation:
  - Connector reports Kubernetes workload/service discovery.
  - CMDB/service catalog returns owner/team.
  - Alert labels provide temporary clues.
  - Admin confirms or corrects the binding.
- Core resource model:

```text
Team owns Service
Service deployed_to DeploymentTarget
DeploymentTarget = cluster + namespace + workload/service identity
Incident targets DeploymentTarget
```

- Admins cannot create a completely nonexistent Service binding without a discovered resource or CMDB/service-catalog record.
- Users should not store free-text `cluster/service/team/namespace` scope lists as their primary permission model.
- First-version role bindings:
  - `team_member`
  - `team_operator`
  - `team_approver`
  - `platform_admin`
  - `platform_auditor`
- Permission resolution chain is User -> Team membership -> owned Services -> DeploymentTargets -> Incident target.
- If the permission chain does not connect, the user is unauthorized unless a platform role explicitly allows access.
- Unbound incidents still appear in `/incidents`.
- Unbound incidents allow only read-only investigation within safely determined alert-label scope.
- Unbound incidents disable execution-class suggested actions and show "需要确认资源归属".
- First-version admin surface is limited to resource binding status in the incident left column, "关联到已有服务/部署目标" for unbound incidents, read-only cluster registration details, and a lightweight edit surface for registered cluster display name, environment, and owner team.
- Mobile first-version scope is responsive incident awareness only: status, conclusion summary, investigation steps, and blocking confirmations. Complex investigation and resource management stay desktop-first.

## Testing Decisions

- Main testing seam: Incident Workbench Gateway API plus Console UI behavior.
- Tests should verify external behavior, not internal storage details.
- The primary acceptance API is `GET /api/incidents/:incidentId/workbench`, together with incident-level investigation mutation APIs and SSE.
- Existing high-level Console/Gateway tests are the closest prior art: incident workbench tests, Agent run SSE tests, evidence query tests, identity/RBAC tests, cluster tests, action/approval/execution tests, and Console web build tests.
- Backend tests should verify the incident-level API shape, including event context, resource binding state, investigation state, evidence steps, current judgment, recommended actions, and report-ready data.
- Backend tests should verify that `investigation/start` returns an existing running thread rather than creating duplicates.
- Backend tests should verify that incident-level investigation APIs can be implemented over existing run/event internals without exposing `/api/agent-runs/*` to the frontend contract.
- Backend tests should verify that message submission creates an immediately visible user message event.
- Backend tests should verify SSE emits the required incident-level event names.
- Backend tests should verify state transitions for `not_started`, `running`, `paused`, `takeover`, `terminated`, `finished`, and `failed`.
- Backend tests should verify takeover stops Agent automatic progression while preserving existing evidence/messages.
- Backend tests should verify Evidence Steps always include readable `title`, `why`, `result_summary`, and `impact`.
- Backend tests should verify failed/skipped/empty evidence states include concrete reasons.
- Backend tests should verify recommended actions cite supporting Evidence Steps.
- Backend tests should verify uncertain or missing evidence prevents confident execution-class recommendations.
- Backend tests should verify redacted samples preserve structured fields and redaction reasons.
- Backend tests should verify raw JSON is not the default evidence payload expected by the UI.
- Backend tests should verify Connector registration is the only source of new `cluster_id`.
- Backend tests should verify admins cannot create a cluster through config upsert with arbitrary Console input.
- Backend tests should verify admins can edit only allowed registered cluster fields.
- Backend tests should verify Connector runtime status cannot be forged by admin configuration.
- Backend tests should verify resource binding resolution through User -> Team -> Service -> DeploymentTarget.
- Backend tests should verify unbound incidents remain visible but disable execution-class actions.
- Backend tests should verify permission-limited data returns explicit hidden/forbidden state rather than fake empty data.
- Frontend tests should verify the route set contains only the first-version routes.
- Frontend tests should verify primary navigation does not show Agent Runs, Runbooks, Approvals, Audit, Policies, Clusters, Users, Settings, Search, or KB.
- Frontend tests should verify the incident detail layout renders compact status bar, left context, center investigation, and right conclusion/actions.
- Frontend tests should verify loading skeletons keep the workbench layout stable.
- Frontend tests should verify no incidents, no evidence, Agent waiting, Connector offline, permission-limited, and error states are readable.
- Frontend tests should verify sending a message immediately renders the user message and waiting state.
- Frontend tests should verify Agent/tool/evidence events update the visible investigation process.
- Frontend tests should verify Evidence Steps are readable without expanding details.
- Frontend tests should verify default views do not render raw JSON/Python dict/K8s object dumps.
- Frontend tests should verify report preview uses known facts, evidence step references, unknowns, notes, and follow-up items.
- Frontend tests should verify mobile/narrow layout stacks conclusion summary, investigation process, and incident context in that order.
- End-to-end smoke should cover: login -> incident list -> incident detail -> start investigation -> see investigation steps and Agent feedback -> send message -> receive Agent/tool/evidence updates -> see evidence-grounded action or blocking confirmation -> generate report.

## Out of Scope

- Automatic production mutation execution.
- Standalone Agent Runs product surface.
- New Agent Run route or Agent Run detail route.
- Runbook management page and enable/disable controls.
- KB candidate approval workflow.
- Global uncontrolled search.
- Standalone approval center.
- Standalone audit center.
- Standalone clusters page.
- Standalone users page.
- Standalone settings page.
- Standalone policies page.
- Full user-management backend.
- Bulk service catalog management.
- Report versioning and publish flow.
- Public unauthenticated report sharing.
- `/btw` side threads.
- Promote side-thread to mainline.
- Multiple parallel discussion threads.
- Token/cost as primary UI elements.
- Raw evidence explorer as default experience.
- Manual cluster creation in the Console.
- Free-text user scope assignment as the primary permission model.
- Complex mobile investigation or resource management workflows.

## Further Notes

- The old implementation at `/root/aiops/AIops` is a reference and source of reusable internals, not the target page model.
- Existing run/event, evidence, cluster runtime, identity, and service ownership concepts can be reused behind a cleaner incident-level API.
- The old `EvidenceNode` and process graph shape should not drive the first-version UI. Evidence Step is the product contract.
- The old config/runtime cluster split is useful, but cluster config must no longer create cluster identity from arbitrary UI input.
- The next planning step should identify which old Gateway APIs are reused as-is, which get thin incident-level adapters, and which old endpoints disappear from the first-version frontend contract.
