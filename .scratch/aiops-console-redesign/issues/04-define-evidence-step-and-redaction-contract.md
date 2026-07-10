Type: grilling
Status: resolved

## Question

What evidence step schema and redaction display contract makes investigation output readable enough for SRE use?

## Context

The agreed direction is to replace the old unreadable process graph with an investigation step list. Each step should show:

- What is being checked.
- Why it is being checked.
- Which data source was queried.
- Result summary.
- Whether it supports, refutes, or leaves uncertain the current hypothesis.
- Raw reference or sanitized snippet when useful.

Redaction must remove secrets and sensitive data without truncating the operational meaning into unreadable fragments.

## Answer

The first-version workbench uses **Evidence Step** as the evidence presentation unit. It replaces unreadable process nodes with investigation steps organized by what the Agent checked and how the result affected the current judgment.

### Evidence Step

Evidence is shown as investigation steps, not as raw data-source nodes.

Example:

```text
[支持当前假设] 检查 checkout 错误率
为什么查：告警显示 5xx 激增，需要确认是否服务自身错误
数据源：OpenObserve metrics
结果：5xx 从 0.2% 升到 8.7%，持续 12 分钟
影响：支持“checkout 服务异常”假设
引用：ev_oo_metrics_xxx
[展开] 查看脱敏样本
```

The default view answers: what was checked, what was found, and why it matters. Raw samples are secondary.

### Schema

Minimum contract:

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

Rules:

- `title`, `why`, `result_summary`, and `impact` must be readable Chinese UI copy.
- `samples` may be empty, but `result_summary` must not be empty.
- `failed` and `skipped` steps must explain why.
- The frontend must not default-render arbitrary JSON for steps.

### Redacted Sample

Use structured redacted samples, not `str[:500]` string truncation:

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

Display rules:

- Default display is `summary` plus a small key-field table.
- Redacted fields show a reason, for example `[已脱敏: token]`; they are not blank.
- Long logs preserve operational context: error type, service name, status code, trace id, duration, and relevant resource identity when allowed.
- Raw references appear as `raw_ref`; raw JSON is not expanded by default.

### Readability Acceptance

These are acceptance criteria:

1. A completed Evidence Step is understandable without expanding details: what was checked, what was found, and how it affected the judgment.
2. User-visible primary text must not expose internal event names or enums such as `tool_call_finished`.
3. Default UI must not show raw JSON, Python dicts, or full Kubernetes objects.
4. Redaction must not leave only `[redacted]`. If a sample cannot be meaningfully displayed, show "该样本因策略不可展示" and explain what summary remains available.
5. `failed`, `skipped`, and `empty` states must show concrete reasons, not generic "暂无数据".

### Relationship To Actions And Reports

Recommended actions must be grounded in evidence:

- Every recommended action references at least one `EvidenceStep.step_id` or evidence ref.
- The action card shows "依据" with 1-3 evidence step titles.
- If the supporting evidence is `uncertain`, the action must be marked "需要人工确认".
- If key evidence failed or is missing, the UI should show a next-check recommendation, not a confident remediation action.
- Incident reports cite evidence steps, not loose evidence ids.
