Type: prototype
Status: resolved

## Question

What exact Incident Workbench information architecture should the first version use, including the three-column layout, content priority, empty/loading/error states, and the event detail default view?

## Context

The agreed direction is:

- Left column: incident context, status, impact, service/cluster/namespace, timeline, human notes.
- Center column: investigation process, Agent output, evidence steps, why each step ran, what it found, what happens next.
- Right column: current conclusion, confidence, missing evidence, recommended actions, human confirmation points, audit summary.

The old UI had oversized titles, low information density, unreadable details, and only a few weak cards.

## Answer

First-version Incident Workbench information architecture:

### Top Status Bar

Do not use a large title card. Use a compact status bar around 72px high:

```text
[SEV2] checkout 5xx 激增     状态: 调查中   影响: 支付成功率下降   时间: 14:03-现在
prod-a / default / checkout   owner: payments   connector: online   [生成报告]
```

The first screen must give most of its vertical space to the workbench, not to headings.

### Desktop Layout

Use a three-column workbench:

```text
左 280px | 中 minmax(560px, 1fr) | 右 360px
```

- Left column is narrow and factual.
- Center column is the primary work area.
- Right column is stable and scan-friendly for conclusion/actions.

On narrow screens, stack in this order:

1. Conclusion summary.
2. Investigation process.
3. Incident context.

### Left Column: Incident Context

Only include:

1. Event facts: status, severity, start time, duration, source, alert fingerprint.
2. Resource context: cluster display name, namespace, service, team, Connector status, binding status.
3. Timeline: alert triggered, investigation started, evidence updated, human notes, status changes.
4. Human notes: SRE-provided facts such as "刚发布过版本".

Do not include raw JSON, full label dumps, all historical runs, or full audit records.

### Center Column: Investigation Process

Evidence and investigation steps come before chat.

Structure:

```text
调查过程
[当前阶段: 收集证据 / 等待确认 / 已完成]
1. 指标异常确认       支持当前假设
2. Pod 重启检查       支持当前假设
3. 依赖调用检查       不确定
4. 日志错误模式       待查询

Agent 交互
[输入框: 继续追问或补充上下文]
消息流：用户问题、Agent 回复、工具调用摘要
```

The SRE should first see what the system checked and what changed the conclusion. Chat is an interaction surface, not the primary evidence view.

### Right Column: Conclusion And Actions

Fixed order:

1. Current judgment: root-cause candidate, confidence, impact scope, unknowns.
2. Recommended actions: each action shows target, risk, why suggested, pre-check, and whether human confirmation is required.
3. Needs your confirmation: missing choices or facts the Agent needs from the human.
4. Responsibility summary: requester, Agent, confirmer/approver, executor, audit status; summary and links only, no JSON payload panels.

### State Rules

These states are acceptance criteria:

- No incidents: show "暂无未恢复事件" and refresh; no empty card grid.
- Loading incident: keep the three-column skeleton so layout does not jump.
- No evidence: say why: not started, query failed, permission denied, or Connector offline.
- Agent running: show phase, elapsed time, last update time, and disable the start button as "调查中".
- Agent has no reply yet: show "等待 Agent 回复" or "Agent 调用失败"; never leave only an input box.
- Connector offline: incident remains readable; actions are disabled with a reason.
- Permission-limited data: show what is hidden; do not render fake empty data.
- Error state: show a readable error summary and retry action; never dump raw JSON as the default UI.
