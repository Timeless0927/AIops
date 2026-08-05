# Tickets: AIOps 通用 Chat 与受治理 Agent 循环

按 [AIOps 通用 Chat 与受治理 Agent 循环规格](./PRD.md) 建立 Chat、受治理工具循环、Decision Trace、显式 Handoff、MCP Integration 和 Skill 管理。先强化现有 Investigation，再让 Chat 成为第二个真实调用方；不合并 SxDevOps，也不新增 Chat Agent 进程。

Work the **frontier**：任一票据的 blockers 全部完成后即可开始。所有票据都使用 fake model/MCP 做确定性验收；真实模型、外部 MCP 和 Kubernetes mutation 留给显式发布验收。

## T01 修复 Diagnosis Observation 回灌与工具活动

**What to build:** 修复 Diagnosis 当前只回灌步骤元数据和短摘要的问题。每次工具调用都转换为结构化、脱敏、受大小限制的 Observation，并把实际关键事实回灌下一轮模型，使后续判断建立在工具结果上。

**Blocked by:** None — can start immediately.

- [ ] Observation 至少包含目的、授权范围、时间范围、状态、关键事实、代表性脱敏样本、截断/脱敏说明和 Evidence reference。
- [ ] 下一轮模型收到规范化 Observation 的实际关键事实和 Evidence reference，而不只是步骤 ID、source、status 或一句 summary。
- [ ] 工具失败、跳过、空结果和超出预算均有结构化结果，不能伪装成成功或静默丢失。
- [ ] 独立来源失败不会抹掉已取得的其他来源；结果状态正确投影为 diagnosed、partial 或 needs-human。
- [ ] 原始日志、完整指标序列、凭据、Secure Input、系统提示词和 Chain of Thought 不进入 Observation。
- [ ] 现有 Diagnosis 外部结果和 writeback 幂等行为保持兼容。
- [ ] 定向 fake MCP/model contract tests 覆盖脱敏、大小限制和部分失败。

**T01 任务记录**

- Module：Diagnosis tool-use session observation feedback；公开 Interface：`run_diagnosis_session` 返回的 `steps`/`tool_activity`，以及现有 `DiagnosisDelivery.accept_writeback`。
- 变更边界：新增 `toolsets/diagnosis_observation.py` 负责 Observation 的脱敏、限长和结构化投影；不新增 trace/store，不扩展 `incident_diagnosis.py`（任务开始已超过 800 行）。
- 定向测试 selector：`pytest -q tests/test_diagnosis_observation.py`、`pytest -q tests/test_gateway_diagnosis_delivery.py -k writeback`、`pytest -q tests/test_diagnosis_llm_tooluse.py`。
- 体量门禁：`diagnosis_session.py` 644 行、`test_diagnosis_llm_tooluse.py` 667 行、`test_gateway_diagnosis_delivery.py` 738 行、`diagnosis_service/jobs.py` 501 行，保持单一职责且不拆分；`incident_diagnosis.py` 1055 行、`test_incident_diagnosis.py` 1004 行本票不新增能力。

## T02 修复 Diagnosis 假设更新、循环停止与答案校验

**What to build:** Diagnosis 显式维护候选原因及其支持/反驳 Evidence，并按“调查目标 → 候选原因 → 工具调用 → Observation → 候选更新 → 继续/停止”循环。模型提出结束后由系统验收；不合规答案最多修复一次，仍失败则返回安全的部分结果。

**Blocked by:** T01 修复 Diagnosis Observation 回灌与工具活动.

- [x] 每轮维护结构化候选原因、支持 Evidence、反驳 Evidence、未知项和下一步检查，不保存模型原始 Chain of Thought。
- [x] 新 Observation 必须更新候选原因关系，明确 supports、refutes 或 uncertain，不能只追加无影响的工具日志。
- [x] 模型只有在当前目标无需更多可用检查时才能提出停止；系统仍检查必需来源、缺失证据、Evidence integrity、事实冲突和剩余预算。
- [x] Investigation 检查必需来源是否成功查询，或是否记录了具体缺失原因。
- [x] 检查 Evidence reference、资源范围、候选原因关系和工具事实冲突。
- [x] 不合规结果最多触发一次带验证原因的 bounded repair。
- [x] repair 仍失败时只返回系统生成的 partial/needs-human 结果，不暴露模型原文或生成 confident diagnosis。
- [x] 定向测试覆盖错误 JSON、错误引用、矛盾结论、缺失来源和 repair 次数上限。

**T02 任务记录**

- Module：Diagnosis governed completion policy；公开 Interface：`run_diagnosis_session` 返回的 `hypothesis_state`、`completion_validation`、`diagnosis` 和终态，以及现有 `DiagnosisDelivery.accept_writeback`。
- 变更边界：新增 `toolsets/diagnosis_completion.py` 维护安全 Hypothesis State、校验停止条件并生成 repair/safe result；复用 T01 Observation、Evidence Step、Tool Activity 和现有 session/writeback，不新增 store、trace 或第二套循环。
- 定向测试 selector：`pytest -q tests/test_diagnosis_completion.py`、`pytest -q tests/test_diagnosis_llm_tooluse.py`、`pytest -q tests/test_diagnosis_observation.py`、`pytest -q tests/test_gateway_diagnosis_delivery.py -k writeback`。
- 体量门禁：任务开始时 `diagnosis_session.py` 675 行、`test_diagnosis_llm_tooluse.py` 667 行；二者职责仍分别为 Diagnosis session 编排和其公开 contract tests，本票把新增确定性策略放入小 Module/独立测试文件，不扩展已超过 800 行的 `incident_diagnosis.py`。
- 完成证据：提交 `0eab96f`；92 个受影响 Module/直接 contract tests 通过，Python 静态语法检查通过；Standards 与 T02 Spec review 均无剩余 finding。任务结束时 `diagnosis_session.py` 791 行，新 `diagnosis_completion.py` 457 行，新 `test_diagnosis_completion.py` 602 行，均未超过 800 行。

## T03 持久化 Diagnosis 循环状态并恢复

**What to build:** Diagnosis Job 持久化安全边界，使服务重启后能继续模型轮次和工具调用而不重复已完成工作，并显式处理在途调用的不确定结果。

**Blocked by:** T01 修复 Diagnosis Observation 回灌与工具活动; T02 修复 Diagnosis 假设更新、循环停止与答案校验.

- [x] 每个安全 checkpoint 记录模型轮次、工具提案、规范化 Observation、Evidence references、剩余预算和完成状态。
- [x] 已完成的工具调用在重启、租约恢复或重复请求中不会再次执行。
- [x] 在途调用没有可信终态时进入可见的 reconciliation/needs-human 状态，不假定成功或静默重试 mutation。
- [x] Diagnosis execution retry 与 writeback retry 保持独立；writeback 失败不重跑已完成 Diagnosis。
- [x] 关闭并重开 Diagnosis store 后，accepted unfinished work 恢复一次且不产生重复结果。
- [x] 迁移保持旧入口的外部结果和幂等语义，不创建第二个长期 owner。

**T03 任务记录**

- Module：Diagnosis Job loop checkpoint and recovery；公开 Interface：`DiagnosisJobs.load_loop_checkpoint` / `save_loop_checkpoint` / `get`、`DiagnosisRuntime.run_job`，以及 `run_diagnosis_job` 的可选 `loop_checkpoint` seam。
- 变更边界：在 Diagnosis-owned `diagnosis_jobs` 增加一个 bounded checkpoint 字段，由 `diagnosis_service/loop_checkpoint.py` 只持久化安全模型提案、规范化 Observation、Evidence reference、预算和完成结果；复用现有 Job execution lease 与独立 writeback retry，不新增数据库、长期 owner、Gateway 状态或第二套工具循环。
- 定向测试 selector：`pytest -q tests/test_diagnosis_checkpoint_recovery.py`、`pytest -q tests/test_diagnosis_jobs.py`、`pytest -q tests/test_diagnosis_completion.py tests/test_diagnosis_observation.py tests/test_diagnosis_llm_tooluse.py`、`pytest -q tests/test_gateway_diagnosis_delivery.py -k writeback`。
- 体量门禁：任务开始时 `diagnosis_service/jobs.py` 502 行，职责仍为 Diagnosis Job durable execution/writeback owner；`toolsets/diagnosis_session.py` 791 行且保持不变。任务结束时共享工作树中的 `jobs.py` 529 行（本票独立提交基线为 520 行）、`runtime.py` 148 行，新 `loop_checkpoint.py` 369 行、新定向测试 321 行，均未超过 800 行。
- 完成证据：提交 `0fec3d1`；T03 recovery/Job tests 与 Gateway writeback contract 通过，Python 静态语法检查通过；固定点 `0eab96f` 已存在的 4 个 LLM tool-use 与 2 个 runtime 失败未增加，Standards 与 T03 Spec 双轴审查无剩余 finding。

## T04 Decision Trace Gateway 投影与 Workbench 展示

**What to build:** 复用现有 Investigation Event、Tool Activity 和 Evidence Step，将调查过程投影为可回放的 Decision Trace，并在 Workbench 展开显示工具目的、范围、结果、Evidence、候选原因影响和停止原因。

**Blocked by:** T01 修复 Diagnosis Observation 回灌与工具活动; T02 修复 Diagnosis 假设更新、循环停止与答案校验.

- [x] diagnosis.output、tool.activity、evidence_step.changed 和 Investigation lifecycle 事件保持原子、幂等、不可变和可 SSE 重放。
- [x] Tool Activity 展示安全目的/范围、状态、耗时、结果摘要、Evidence references、对候选原因的支持/反驳影响和继续/停止原因。
- [x] 当前判断展示候选原因、支持/反驳的 Evidence、置信度提示和 Evidence Gate 状态；置信度不替代 Gate。
- [x] failed、skipped、truncated、redacted 和 missing 状态显示具体原因。
- [x] 默认视图不显示 Chain of Thought、prompt、credential、Secure Input、未脱敏样本或任意 raw JSON。
- [x] 刷新、SSE 断线重连和历史分页都能恢复相同 Decision Trace。

**T04 任务记录**

- Module：Gateway Decision Trace projection 与 Console Workbench presentation；公开 Interface：`project_decision_trace` / `project_diagnosis_output` 生成的 `diagnosis.output`、`tool.activity` 事件 payload，以及 Console `DecisionTrace` / `decisionTraceFromEvents`。
- 变更边界：复用 `DiagnosisDelivery.accept_writeback` 的单一 transaction、immutable Investigation Event、现有 SSE cursor 与 `listInvestigationEvents` 全分页读取；不新增 trace table/store，不修改 Evidence Gate，不暴露 Diagnosis raw payload。
- 定向测试 selector：`pytest -q tests/test_gateway_decision_trace.py tests/test_gateway_diagnosis_delivery.py tests/test_gateway_investigation_events.py`、`npm test -- src/prototype/decision-trace.test.tsx src/prototype/investigation-event-state.test.ts`、Console `npm run build`。
- 体量门禁：任务开始时 `diagnosis_delivery.py` 457 行、`workbench-page.tsx` 410 行、`test_gateway_diagnosis_delivery.py` 751 行（共享工作树）；任务结束时分别为 466、413、751 行。新 `decision_trace.py` 242 行、新 Gateway test 214 行、新 Console component/test 234/124 行，均未超过 800 行。
- 完成证据：提交 `be9ab9a`；Gateway Decision Trace/Diagnosis Delivery/Investigation Event 定向测试与 Console Decision Trace/SSE state 定向测试通过，Python 语法检查、TypeScript no-emit 和 production build 通过；TDD 红灯先证明原 writeback 会持久化并回放 credential、Secure Input、sample/raw payload，绿灯后 SQLite 和事件均只保留安全投影；双轴审查确认的状态原因展示缺口已由第二轮红绿测试修复，Standards 与 T04 Spec 无剩余 finding。

## T05 Chat Session 与基础 Chat 页面

**What to build:** User 可以在 Console 打开私有 Chat Session，发送知识问题、查看历史和恢复未完成消息；知识问题可以不调用真实环境工具。

**Blocked by:** T04 Decision Trace Gateway 投影与 Workbench 展示.

- [x] Chat Session 和消息由 Gateway 持久化，默认仅创建者可见，普通 Chat 内容按 30 天固定策略保留。
- [x] Console 提供一级 Chat 工作入口，页面包含会话列表、消息流、发送中、失败重试和断线恢复状态。
- [x] 未登录、无权读取、过期或不存在的 Session 返回明确且不泄露资源存在性的结果。
- [x] 知识问题可以直接回答，不强制调用工具；回答不会产生 Evidence、Approval 或 Connector Command。
- [x] 消息发送后立即可见，Gateway/SSE 事件可在刷新后恢复，不建立浏览器端第二套会话状态机。
- [x] fake model Gateway contract、Console behavior、TypeScript no-emit 和 production build 通过。

**T05 任务记录**

- Module：Gateway Chat Session/message owner、Diagnosis knowledge response 与 Console Chat presentation；公开 Interface：`ChatSessions.create` / `list` / `get` / `send` / `retry` / `list_events`，Gateway `/api/v1/chat/sessions*` HTTP/SSE contract，Diagnosis `answer_knowledge_chat`，Console `ChatPage`。
- 变更边界：Chat Session 与消息只存 Gateway `gateway.db`，固定 30 天保留且 creator-only；知识回答通过 Gateway→Diagnosis 内部 HTTP seam 且 tools 为空；不新增 Chat 进程，不引入资源 scope、MCP、Evidence、Approval、Command、Handoff 或共享工具循环（均留给 T06/T07）。
- 定向测试 selector：`pytest -q tests/test_gateway_chat.py tests/test_gateway_v1_chat_contract.py tests/test_diagnosis_knowledge_chat.py`、`pytest -q tests/test_gateway_v1_auth_contract.py::test_bootstrap_cookie_session_and_empty_incident_contract`、`npm test -- src/chat/chat-page.test.tsx src/shell/console-shell.test.tsx src/api/client.test.ts`、Console `npm run generate:api && npm run build`。
- 体量门禁：任务开始时 `apps/aiops_k8s_gateway/main.py` 795 行、`diagnosis_service/service_main.py` 546 行、Console `client.ts` 457 行、`App.tsx` 85 行、`console-shell.tsx` 259 行；HTTP/entry 文件只做装配与分发，新增领域能力进入独立 Module，OpenAPI/generated types 属 contract artifact。
- 体量结果：任务结束时上述文件分别为 798/548/493/88/289 行；新 Gateway Chat owner/HTTP adapter 367/172 行，新 Diagnosis knowledge policy/HTTP adapter 43/45 行，新 Console Chat page/test 193/73 行，均未超过 800 行。
- 完成证据：Gateway/Diagnosis 8 项 Chat 定向测试、Gateway migration/auth 直接 contract 与 Console 13 项行为/client 测试通过，OpenAPI JSON 和生成类型一致，Python 语法检查、TypeScript no-emit 与 production build 通过；TDD 红灯依次覆盖缺少持久 owner、失败恢复/脱敏、无工具模型 seam、公开 HTTP/SSE/OpenAPI、creator-only stream 和 Console 状态，绿灯后均由公开 Interface 验证；双轴审查确认的断线/恢复状态缺口已由第二轮红绿测试修复，Standards 与 T05 Spec 无剩余 finding。

## T06 环境 Chat 与共享受治理循环

**What to build:** Chat 成为 Diagnosis 循环的第二个真实调用方；用户选定资源后，Chat 可以查询授权的只读 MCP，环境结论必须引用有效 Observation，且与 Investigation 共用预算、恢复和冲突检查。

**Blocked by:** T01 修复 Diagnosis Observation 回灌与工具活动; T02 修复 Diagnosis 假设更新、循环停止与答案校验; T03 持久化 Diagnosis 循环状态并恢复; T05 Chat Session 与基础 Chat 页面.

- [x] Gateway 为每次环境 Chat 冻结 User 的 RBAC 资源范围，模型只能缩小不能扩大。
- [x] 未选择真实资源时只能回答通用知识，不能查询真实集群。
- [x] 环境问题没有成功授权 Observation 时不得给出确定的 live-state 结论。
- [x] disabled、unknown、changed、mutation-like 或越权工具 fail closed。
- [x] Chat 和 Investigation 使用同一受治理 loop policy、Observation、completion validation、checkpoint 和恢复逻辑，但保持不同会话对象。
- [x] fake model/MCP HTTP/SSE smoke 覆盖知识无工具、环境有工具、越权、预算耗尽和重启恢复。

**T06 任务记录**

- Blocker 证据：T03 `0fec3d1`、T05 `500794a` 已完成；T01/T02/T03 Observation、completion policy 与 checkpoint 是本票复用基线。
- Module：Gateway Chat scope authorization/message projection 与 Diagnosis governed Chat execution；公开 Interface：`freeze_chat_scope`、扩展后的 `ChatSessions.send` / `retry`、Gateway `/api/v1/chat/sessions*` HTTP/SSE，Diagnosis `answer_governed_chat`、`run_diagnosis_session(profile=...)`、`GovernedLoopCheckpoint` 与 `ChatLoopCheckpoints`。
- 变更边界：Gateway 从当前 actor 可见的 Resource Catalog/Incident 投影冻结每次请求的 scope；Diagnosis 复用同一 loop、Observation、completion/checkpoint，仅增加 knowledge/environment policy profile 和静态只读 capability snapshot；不增加 Chat 进程，不创建 Approval、Execution Grant、Connector mutation、Handoff、动态 MCP Registry 或 Skill。
- 定向测试 selector：`pytest -q tests/test_gateway_chat_scope.py tests/test_gateway_chat.py tests/test_gateway_v1_chat_contract.py tests/test_diagnosis_governed_chat.py`、`pytest -q tests/test_diagnosis_observation.py tests/test_diagnosis_completion.py tests/test_diagnosis_checkpoint_recovery.py tests/test_diagnosis_llm_tooluse.py`、`npm test -- src/chat/chat-page.test.tsx src/api/client.test.ts`、Console `npm run generate:api && npm run build`。
- 体量门禁：任务开始时 `diagnosis_session.py` 791 行、`diagnosis_completion.py` 457 行、`loop_checkpoint.py` 369 行、`jobs.py` 529 行、Gateway `chat_sessions.py` 367 行、`chat_http.py` 172 行、`main.py` 798 行、Diagnosis `service_main.py` 548 行、Console `chat-page.tsx` 193 行、`client.ts` 493 行；`main.py` 仅保持装配且不得超过 800 行，`jobs.py` 的共享工作树修改不纳入本票。
- 最终体量：`diagnosis_session.py` 750 行、`diagnosis_completion.py` 551 行、`loop_checkpoint.py` 368 行、`chat_sessions.py` 529 行、`chat_http.py` 206 行、`main.py` 798 行、`service_main.py` 554 行、`chat-page.tsx` 240 行、`client.ts` 499 行；超过 500 行的文件仍分别保持 completion、Chat persistence、Diagnosis 装配等单一 Module 职责，公开 Interface 与上述 selector 已确认，且没有文件超过 800 行或在超 800 行入口新增领域行为。
- 验证：Gateway/Diagnosis Chat Module 与 fake HTTP/SSE `25 passed`，共享 Observation/completion/checkpoint/loop contract `39 passed`，Gateway migration/auth contract `1 passed`；Console Chat/client/shell `14 passed`，`generate:api`、`tsc --noEmit` 与 Vite build 通过，Python `py_compile`、OpenAPI `jq empty`、staged `diff --check` 通过。
- 双轴审查：Standards 轴确认并修复 Diagnosis frozen `selection` 输入校验缺口；Spec 轴确认环境 retry 必须按当前 actor 重新授权且仅在冻结快照未变化时恢复，变化时 `chat_scope_changed` fail closed。Tool Activity 在 Gateway 仅持久化 OpenAPI 白名单字段；静态只读 capability snapshot 是 T08 前明确边界，未提前实现 Handoff、动态 MCP Registry、Skill 或任何 mutation/Approval 能力。

## T07 显式 Handoff 与 User-created Incident

**What to build:** User 可以明确选择 Chat 消息，把上下文作为 Human Input 关联已有 Incident，或在没有 Alert Signal 时创建带真实资源范围的 User-created Incident 和第一轮 Investigation。

**Blocked by:** T05 Chat Session 与基础 Chat 页面; T06 环境 Chat 与共享受治理循环.

- [x] Handoff 必须是显式、幂等、可审计的 User 命令，不能由模型自动创建。
- [x] User 可选择目标 Incident；已有 Incident 的 Investigation、权限、Evidence Gate 和生命周期规则保持不变。
- [x] 没有 Alert Signal 时，创建 User-created Incident 必须提供真实 Cluster/namespace/Service/Deployment Target 范围和问题摘要。
- [x] 只有被选消息被复制为 Human Input；未选消息不转移，任何 Chat 内容都不是 Evidence。
- [x] Handoff 不创建 Approval、Execution Grant 或 Connector Command，且受当前 actor/team scope 控制。
- [x] Console 显示确认、成功、重复、越权、目标不存在和资源未绑定等状态；Gateway HTTP/SSE contract 覆盖新旧 Incident 两条路径。

**T07 任务记录**

- Blocker 证据：T05 `500794a`、T06 `906231e` 已完成；T07 只接入既有 Chat Session、Incident、Investigation Event/Human Input 与 Diagnosis Request owner。
- Module：Gateway `ChatHandoffs` 显式命令、Incident `create_user_incident_in` 共享事务 Interface 与 Chat Handoff HTTP/OpenAPI Adapter，Console Chat transient confirmation；公开 Interface：`ChatHandoffs.execute`、`create_user_incident_in`、`POST /api/v1/chat/sessions/{session_id}/handoffs`、既有 Chat/Investigation SSE replay。
- 变更边界：Handoff 只复制选中且已完成的 Chat message content 为 `human_input.assertion`；已有 Incident 不改变 Investigation lifecycle，User-created Incident 复用同一 Incident/Investigation/Diagnosis Request 表与治理链；不创建 Evidence、Approval、Execution Grant、Connector Command，不实现 T08/T09 Registry/Skill。
- 定向测试 selector：`pytest -q tests/test_gateway_chat_handoff.py tests/test_gateway_v1_chat_handoff_contract.py`、直接 contract `tests/test_gateway_chat.py tests/test_gateway_investigation_events.py tests/test_gateway_diagnosis_delivery.py`、Console `npm test -- --run src/chat/chat-page.test.tsx src/api/client.test.ts` 与 `npm run generate:api && npm run build`。
- 体量门禁：任务开始时 `incident.py` 728 行、`investigation_events.py` 398 行、`chat_sessions.py` 529 行、`chat_http.py` 206 行、Gateway `main.py` 798 行、Console `chat-page.tsx` 240 行、`client.ts` 499 行；新 Handoff 能力进入独立 Module，`main.py` 只允许装配且保持低于 800 行。
- 最终体量：`incident.py` 794 行、`chat_handoffs.py` 284 行、`chat_http.py` 248 行、Gateway `main.py` 799 行、Console `chat-page.tsx` 345 行、`client.ts` 514 行；超过 500 行的 `incident.py`/`client.ts` 仍分别保持 Incident owner/API client 单一职责，公开 Interface 与上述 selector 已确认，且没有文件超过 800 行。
- 验证：Handoff Module 与 Gateway HTTP/SSE `5 passed`，直接 Chat/Investigation Event/Diagnosis Delivery contract `18 passed`，Incident/Auth/Migration contract `7 passed`，Console Chat/client `14 passed`；OpenAPI JSON、Python `py_compile`、`generate:api`、TypeScript no-emit 与 Vite production build 通过。
- 双轴审查：Standards 轴发现并修复 ChatHandoffs 跨 owner 直写 Incident/Investigation 的问题，创建路径已移至 Incident Module 的 `create_user_incident_in` 共享事务 Interface；Spec 轴其余要求通过。不存在与 team-scope 越权继续统一为非枚举 `handoff_target_not_found`，Console 同时对 capability 403 显示独立越权状态，避免通过差异文案泄露 Incident 存在性。

## T08 MCP Integration Registry 管理页

**What to build:** Platform Administrator 可以注册、验证、启停和审计 MCP Integration，并明确其只读能力、允许范围、健康状态和凭据状态；未经 AIOps policy 允许的能力对 Chat 不可用。

**Blocked by:** T06 环境 Chat 与共享受治理循环.

- [x] Admin 可以注册/更新/验证 MCP Integration，查看 health、capability snapshot、版本变化和允许范围。
- [x] 未验证、已禁用、能力变更、未知或 mutation-like tool 默认 fail closed。
- [x] 只有显式启用且被 AIOps 标记为 read-only 的工具可被 Chat/Investigation 使用。
- [x] 凭据使用现有 secure administration、fresh-auth、masking 和审计规则，plaintext 不进入 API、事件、日志或 Console state。
- [x] 新旧 capability snapshot、enable/disable、verification 和使用拒绝都有 actor/request 审计。
- [x] Admin API、Console tab、健康/失败状态和 fake MCP contract tests 完整，不把状态塞入 GatewayV1Store 总命名空间。

**T08 任务记录**

- Blocker 证据：T06 `906231e` 已完成受治理 environment Chat；T08 只把静态 MCP capability snapshot 收敛为管理员治理的 Registry，不实现 T09 Skill。
- Module：Gateway `MCPRegistry` 独立 owner、MCP Registry HTTP/OpenAPI Adapter、Diagnosis capability authorization 与 Console Admin MCP tab；公开 Interface：`MCPRegistry.create/update/verify/list/authorized_snapshot`、`/api/v1/admin/mcp-integrations*`、扩展后的 Diagnosis Request/Chat capability contract。
- 变更边界：Registry 只管理 endpoint、加密 credential、health、exact capability snapshot、AIOps read-only policy、allowed scope、enablement 和审计；不创建新进程、不增加 mutation tool、不让 Browser 直连 MCP、不实现 Skill 或通用 plugin 平台。
- 定向测试 selector：`pytest -q tests/test_gateway_mcp_registry.py tests/test_gateway_v1_mcp_registry_contract.py tests/test_diagnosis_governed_chat.py tests/test_diagnosis_runtime.py`、直接 contract `tests/test_gateway_chat.py tests/test_gateway_diagnosis_delivery.py tests/test_observability_mcp_runtime.py`、Console `npm test -- --run src/admin/mcp-registry-admin.test.tsx src/api/client.test.ts src/admin/admin-page.test.tsx` 与 `npm run generate:api && npm run build`。
- 体量门禁：任务开始时 Gateway `main.py` 799 行、Console `admin-page.tsx` 325 行、`client.ts` 514 行、`governed_tools.py` 40 行、`chat_http.py` 248 行、Diagnosis `runtime.py` 148 行、`service_main.py` 554 行、`observability_http.py` 153 行；新 Registry 进入独立 Module，`main.py` 仅装配且必须保持低于 800 行。
- TDD：先加入 Registry domain/HTTP/fake MCP/delivery 与 Console API/View 红测；部署核验随后暴露 Gateway→MCP ingress 和 encryption key 缺口，admin audit strict contract 红测暴露内部 `before_json/after_json` 泄漏；逐片实现后全部转绿。
- 部署 Interface：bootstrap 生成 `aiops-mcp-encryption`，Pilot 只挂载到 Gateway 并纳入 readiness/RBAC；三个 MCP NetworkPolicy 同时允许既有 Diagnosis 与新的 Gateway caller。定向 selector：`tests/test_gateway_mcp_deployment.py tests/test_bootstrap_service.py tests/test_pilot_release.py`。
- 最终体量：`mcp_registry.py` 642 行、`mcp_registry_http.py` 166 行、`mcp-registry-admin.tsx` 317 行、`main.py` 799 行、`client.ts` 547 行、Diagnosis `service_main.py` 596 行；开始时 556 行的 `tests/test_diagnosis_service.py` 归属 Diagnosis HTTP Adapter contract，最终 599 行，selector 为 `tests/test_diagnosis_service.py`。均未超过 800 行，Gateway entrypoint 未增长。
- 验证：后端/直接 contract/deployment 组合 `73 passed`；Console `15 passed`；`npm run build`、OpenAPI JSON、受影响 Python compile 通过。build 保留既有主 chunk 大小 warning，本票未增加依赖或构建配置。
- 双轴审查：Standards 轴无硬问题，Spec 轴无缺口、scope creep 或行为错误。Standards 的唯一 judgement call 是 `main.py` 重复构造无状态 Registry；没有行为收益且会增加 entrypoint 装配，按 Ponytail 保持现状。Spec residual risk 是未来不同工具参数形状会被 scope 校验 fail closed，不属于 T08 当前 contract。

## T09 Skill 版本化管理页

**What to build:** Platform Administrator 可以管理非可执行、版本化 Skill，并声明其依赖的 MCP Integration 和适用范围；Skill 只能引用已启用能力，不能运行代码或扩大 User 权限。

**Blocked by:** T06 环境 Chat 与共享受治理循环; T08 MCP Integration Registry 管理页.

- [ ] Admin 可以创建、查看、版本化、启用和禁用 Skill，历史版本保持可审计。
- [ ] Skill 只能保存 instruction/workflow 内容、适用范围和 required MCP references，不接受脚本、插件包或 arbitrary executable code。
- [ ] Enable 前验证 MCP 依赖、capability snapshot、scope 和版本；依赖漂移或禁用时新调用 fail closed。
- [ ] Skill 不能授予权限、扩大范围、创建 Approval 或触发 mutation。
- [ ] Tool Activity/Decision Trace 记录实际使用的 Skill version，禁用后不改写历史记录。
- [ ] Admin API、Console tab、依赖失败、版本切换、审计和 fake model/MCP contract tests 完整。
