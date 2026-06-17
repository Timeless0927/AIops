# AIOps Console V1 Information Architecture and Gateway API Contract

Date: 2026-06-12

Status: Proposed for AIO-87 frontend handoff

Related issues: AIO-73, AIO-80, AIO-84, AIO-85, AIO-86, AIO-88, AIO-89, AIO-90, AIO-91, AIO-92

## Scope

This document defines the V1 information architecture, page map, role boundary,
Gateway API contract, and page state model for AIOps Console.

It is a frontend implementation dependency contract. It does not implement UI
pages, backend handlers, Hermes integration, Connector calls, MCP calls, or
mutation execution.

## Architecture Boundary

Console V1 has one frontend data boundary:

```text
Browser -> AIOps Gateway / control-plane API -> internal services and stores
```

Forbidden frontend paths:

- Browser -> Hermes
- Browser -> Connector
- Browser -> MCP services
- Browser -> Prometheus / Loki direct API
- Browser -> Feishu approval API or Feishu webhook

Approval Center uses only Gateway/control-plane Approval Service. Feishu is a
notification and deep-link channel only. Feishu messages may include links to
Console, but Feishu never owns approval state and never changes approval state.

Grafana is display compatibility only. Console may render a Gateway-provided
Grafana embed URL or fallback, but it must not mint sensitive Grafana tokens in
the browser or bypass the enterprise Grafana permission model.

## V1 Information Architecture

Primary navigation:

| Area | Route | Owner issue | Purpose |
| --- | --- | --- | --- |
| Incidents | `/incidents` | AIO-88 | Searchable alert/incident history. |
| Incident detail | `/incidents/:incident_id` | AIO-89 | Timeline, diagnosis, evidence, approval, Grafana, audit, cost. |
| Approval Center | `/approvals/pending`, `/approvals/history`, `/approvals/:approval_id` | AIO-91 | Internal pending approvals and approval history. |
| Cost | `/costs` | AIO-92 | Cost overview by time range, team, service, incident. |
| Management | `/settings/notifications`, `/settings/ownership` | Later V1 admin slice | Notification channel and ownership validation entry points. |

Secondary cross-links:

- Incident list row -> incident detail.
- Incident detail approval panel -> approval detail.
- Approval detail incident summary -> incident detail.
- Incident detail cost panel -> cost drill-down filtered by incident.
- Cost overview drill-down -> incident detail or session detail.
- Notification/ownership settings -> read-only validation and correction entry
  points; full configuration center is not in V1.

## Roles and Permissions

Gateway is the final authorization authority. Frontend checks only control
visibility and ergonomics; API calls must still be rejected server-side when
unauthorized.

| Role | View incidents | View evidence | View costs | Approve/reject | Manage notification channels | Manage ownership mappings |
| --- | --- | --- | --- | --- | --- | --- |
| `viewer` / ordinary user | Authorized service/team/namespace scope only | Same scope; raw refs may be redacted | Same scope | No | No | No |
| `oncall` | Authorized on-call scope | Same scope | Same scope | Only when also policy-eligible and `can_approve=true` | No | No |
| `approver` | Authorized approval scope | Same scope | Same scope | Yes, only for matching scope and pending request | No | No |
| `admin` | Global | Global, with audit | Global | Can approve only if policy allows; can cancel | Yes | Validate/correct local mapping hints only |

Permission dimensions:

- `service_id`
- `team_id`
- `namespace`
- `environment`
- `risk_level`
- `action_type`

Every response that backs a user action should include `permissions` or specific
capability fields such as `can_approve`, `can_cancel`, and `blocked_reason`.

## Common API Conventions

Base path: `/api`

Envelope:

```json
{
  "data": {},
  "meta": {
    "request_id": "req_...",
    "generated_at": "2026-06-12T09:30:00Z"
  }
}
```

List envelope:

```json
{
  "data": [],
  "page": {
    "page": 1,
    "page_size": 50,
    "total": 123,
    "has_more": true,
    "next_cursor": "optional-cursor"
  },
  "meta": {
    "request_id": "req_...",
    "generated_at": "2026-06-12T09:30:00Z"
  }
}
```

Error envelope:

```json
{
  "error": {
    "code": "forbidden",
    "message": "You do not have access to this service.",
    "reason": "scope_mismatch",
    "retryable": false,
    "request_id": "req_..."
  }
}
```

Common query parameters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `page` | integer | 1-based page for normal tables. |
| `page_size` | integer | Default 50, max 100. |
| `cursor` | string | Optional for timeline/cost drill-down streams. |
| `sort` | string | Field name. Prefix with `-` for descending. |
| `time_from`, `time_to` | RFC3339 | Required for cost and Grafana context; optional elsewhere. |

Common HTTP status handling:

| Status | Frontend treatment |
| --- | --- |
| `200` | Render data. |
| `201` | Render created resource or optimistic success. |
| `202` | Show accepted/in-progress state and refresh status. |
| `400` | Show field-level validation. |
| `401` | Show login/session-expired state. |
| `403` | Show unauthorized state with `blocked_reason`. |
| `404` | Show missing or no-scope resource state; avoid leaking existence when needed. |
| `409` | Show conflict/stale state and refresh resource. |
| `410` | Show expired terminal state. |
| `422` | Show domain validation failure. |
| `429` | Show retry-after state. |
| `5xx` | Show retryable error state with request id. |

## Page Map and API Dependencies

### AIO-88: Incident History

Route: `/incidents`

Primary API:

```http
GET /api/incidents
```

Filters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `q` | string | Search title, incident id, service name, alert labels. |
| `service_id` | string | Exact service filter. |
| `team_id` | string | Owner team filter. |
| `severity` | csv enum | `critical,high,medium,low,info`. |
| `status` | csv enum | `open,diagnosing,waiting_approval,approved,rejected,resolved,failed,closed`. |
| `diagnosis_status` | csv enum | `not_started,running,succeeded,partial,failed`. |
| `approval_status` | csv enum | `none,pending,approved,rejected,expired,cancelled`. |
| `ownership` | enum | `owned,unowned,default_team`. |
| `time_from`, `time_to` | RFC3339 | Based on first or latest occurrence. |
| `sort` | string | `-last_seen_at`, `severity`, `status`, `service_name`. |
| `page`, `page_size` | integer | Default page size 50. |

List item fields:

```json
{
  "incident_id": "inc_...",
  "title": "payment-api 5xx spike",
  "service_id": "svc_payment_api",
  "service_name": "payment-api",
  "owner_team_id": "team_payments",
  "owner_team_name": "Payments",
  "ownership_status": "owned",
  "ownership_source": "cmdb",
  "ownership_confidence": 0.92,
  "severity": "high",
  "status": "diagnosing",
  "first_seen_at": "2026-06-12T08:00:00Z",
  "last_seen_at": "2026-06-12T08:05:00Z",
  "repeat_count": 4,
  "diagnosis_status": "partial",
  "approval_status": "none",
  "latest_session_id": "sess_...",
  "permissions": {
    "can_view": true,
    "can_view_cost": true,
    "can_approve": false,
    "blocked_reason": null
  }
}
```

Page states:

- Loading: table skeleton plus disabled filters.
- Empty unfiltered: no incidents have been ingested.
- Empty filtered: no incident matches current filters; keep filters visible.
- Error: show request id and retry.
- Unauthorized: show no-scope message; do not show counts from forbidden scopes.

### AIO-89: Incident Detail, Timeline, Diagnosis, Evidence

Route: `/incidents/:incident_id`

Primary APIs:

```http
GET /api/incidents/{incident_id}
GET /api/incidents/{incident_id}/timeline
GET /api/incidents/{incident_id}/evidence
GET /api/incidents/{incident_id}/diagnosis
GET /api/incidents/{incident_id}/audit
```

Detail fields:

```json
{
  "incident_id": "inc_...",
  "title": "payment-api 5xx spike",
  "status": "diagnosing",
  "severity": "high",
  "source": {
    "kind": "alertmanager",
    "alert_id": "alert_...",
    "labels": {
      "service": "payment-api",
      "namespace": "prod"
    }
  },
  "service": {
    "service_id": "svc_payment_api",
    "service_name": "payment-api",
    "owner_team_id": "team_payments",
    "owner_team_name": "Payments",
    "ownership_status": "owned"
  },
  "latest_session_id": "sess_...",
  "created_at": "2026-06-12T08:00:00Z",
  "updated_at": "2026-06-12T08:05:00Z",
  "permissions": {
    "can_view": true,
    "can_view_raw_evidence": false,
    "can_view_cost": true,
    "can_approve": false
  }
}
```

Timeline filters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `type` | csv enum | `alert,session,evidence,diagnosis,approval,notification,execution,audit`. |
| `cursor` | string | Cursor pagination. |
| `page_size` | integer | Default 100. |

Timeline item fields:

```json
{
  "event_id": "evt_...",
  "occurred_at": "2026-06-12T08:04:00Z",
  "type": "evidence",
  "status": "partial",
  "title": "K8s read returned no matching pods",
  "summary": "Selector returned zero resources.",
  "actor": {
    "type": "system",
    "id": "gateway"
  },
  "refs": {
    "request_id": "req_...",
    "session_id": "sess_...",
    "evidence_id": "ev_k8s_..."
  }
}
```

Diagnosis fields:

```json
{
  "session_id": "sess_...",
  "status": "partial",
  "summary": "High 5xx rate correlated with payment-api logs.",
  "root_cause": {
    "category": "insufficient_evidence",
    "statement": "Undifferentiated incident requiring more evidence.",
    "confidence": 0.52
  },
  "diagnosed_at": "2026-06-12T08:04:30Z",
  "evidence_summary": [
    {
      "evidence_id": "ev_prom_...",
      "kind": "prometheus",
      "status": "succeeded",
      "summary": "5xx rate elevated."
    }
  ],
  "action_proposals": [
    {
      "action_proposal_id": "act_...",
      "summary": "Restart payment-api deployment",
      "risk_level": "high",
      "approval_required": true,
      "approval_id": null,
      "execution_enabled": false
    }
  ],
  "redactions": {
    "chain_of_thought_hidden": true,
    "raw_evidence_restricted": true
  }
}
```

Evidence filters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `kind` | csv enum | `prometheus,loki,k8s,topology,grafana`. |
| `status` | csv enum | `succeeded,partial,skipped,failed,empty`. |
| `include_raw` | boolean | Gateway may ignore unless user has raw evidence permission. |

Evidence item fields:

```json
{
  "evidence_id": "ev_...",
  "kind": "k8s",
  "status": "empty",
  "summary": "No pods matched selector.",
  "collected_at": "2026-06-12T08:03:00Z",
  "query": {
    "display": "namespace=prod selector=app.kubernetes.io/name=payment-api",
    "time_range": {
      "from": "2026-06-12T07:45:00Z",
      "to": "2026-06-12T08:05:00Z"
    }
  },
  "result_ref": "result_...",
  "raw_available": false,
  "failure": {
    "code": "no_matching_resources",
    "message": "Selector returned zero resources.",
    "retryable": false
  }
}
```

Page states:

- Loading: header skeleton plus independent panel skeletons.
- Empty: incident exists but no diagnosis session yet.
- Partial: render successful evidence and mark skipped/failed evidence clearly.
- Diagnosis failed: show failure summary and timeline/audit refs.
- Unauthorized: show resource-level forbidden state; do not reveal hidden evidence.

### AIO-90: Grafana Embed Compatibility

Grafana URLs are produced by Gateway from configured templates and incident
context. The frontend receives only render-safe URLs or fallback metadata.

Primary APIs:

```http
GET /api/incidents/{incident_id}/grafana-panels
GET /api/grafana/panels?service_id=...&time_from=...&time_to=...
```

Fields:

```json
{
  "panels": [
    {
      "panel_id": "grafana_payment_5xx",
      "title": "HTTP 5xx rate",
      "mode": "iframe",
      "embed_url": "https://grafana.example/d-solo/...",
      "fallback_url": "https://grafana.example/d/...",
      "variables": {
        "service": "payment-api",
        "namespace": "prod"
      },
      "time_range": {
        "from": "2026-06-12T07:45:00Z",
        "to": "2026-06-12T08:05:00Z"
      },
      "status": "available",
      "unavailable_reason": null
    }
  ]
}
```

Fallback modes:

| Mode | Meaning |
| --- | --- |
| `iframe` | Embed URL is allowed by Grafana CSP/auth. |
| `link` | Show link to Grafana; embedding not allowed. |
| `snapshot` | Show static snapshot URL if generated server-side. |
| `rendered_image` | Show rendered image if Gateway has safe render API integration. |
| `unavailable` | Show configured missing/unauthorized state. |

Page states:

- Loading: fixed panel placeholders.
- Empty: no panel mapping for service/team.
- Unauthorized: user can see incident but not Grafana panel.
- Error: embed failed; keep fallback link if present.

### AIO-91: Internal Approval Center

Routes:

- `/approvals/pending`
- `/approvals/history`
- `/approvals/:approval_id`

Primary APIs:

```http
GET /api/approval-requests
GET /api/approval-requests/{approval_id}
POST /api/approval-requests/{approval_id}/approve
POST /api/approval-requests/{approval_id}/reject
POST /api/approval-requests/{approval_id}/cancel
```

List filters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `status` | csv enum | `pending,approved,rejected,expired,cancelled`. |
| `assigned_to` | enum/string | `me` for pending inbox; explicit user id for admin. |
| `scope` | enum | `me,team,global`; Gateway enforces role. |
| `team_id` | string | Team history filter. |
| `service_id` | string | Service filter. |
| `incident_id` | string | Incident filter. |
| `risk_level` | csv enum | `low,medium,high,critical`. |
| `created_from`, `created_to` | RFC3339 | Request creation range. |
| `page`, `page_size`, `sort` | mixed | Default sort `-created_at`. |

List item fields:

```json
{
  "approval_id": "appr_...",
  "incident_id": "inc_...",
  "session_id": "sess_...",
  "action_proposal_id": "act_...",
  "service_id": "svc_payment_api",
  "service_name": "payment-api",
  "owner_team_id": "team_payments",
  "owner_team_name": "Payments",
  "risk_level": "high",
  "action_summary": "Restart payment-api deployment",
  "requested_by": {
    "type": "system",
    "id": "hermes"
  },
  "created_at": "2026-06-12T08:06:00Z",
  "expires_at": "2026-06-12T08:36:00Z",
  "status": "pending",
  "can_approve": true,
  "can_reject": true,
  "blocked_reason": null
}
```

Detail fields:

```json
{
  "approval_id": "appr_...",
  "incident": {
    "incident_id": "inc_...",
    "title": "payment-api 5xx spike",
    "severity": "high"
  },
  "service": {
    "service_id": "svc_payment_api",
    "service_name": "payment-api",
    "owner_team_id": "team_payments"
  },
  "diagnosis_summary": "High 5xx rate correlated with payment-api logs.",
  "evidence_refs": [
    {
      "evidence_id": "ev_prom_...",
      "kind": "prometheus",
      "summary": "5xx rate elevated."
    }
  ],
  "action_proposal": {
    "action_proposal_id": "act_...",
    "action_type": "k8s_restart",
    "summary": "Restart payment-api deployment",
    "approval_required": true
  },
  "resource_scope": {
    "cluster": "prod",
    "namespace": "payments",
    "workload": "deployment/payment-api"
  },
  "risk_level": "high",
  "risk_explanation": "Restart may temporarily reduce capacity.",
  "rollback_plan": "Rollout undo deployment/payment-api if post-check fails.",
  "audit_refs": [
    {
      "audit_id": "aud_...",
      "kind": "approval_requested"
    }
  ],
  "decision_history": [
    {
      "status": "pending",
      "actor": {
        "type": "system",
        "id": "gateway"
      },
      "at": "2026-06-12T08:06:00Z",
      "reason": null
    }
  ],
  "status": "pending",
  "expires_at": "2026-06-12T08:36:00Z",
  "can_approve": true,
  "can_reject": true,
  "can_cancel": false,
  "blocked_reason": null
}
```

Approve request:

```json
{
  "comment": "Approved for controlled maintenance window.",
  "idempotency_key": "client-generated-uuid"
}
```

Reject request:

```json
{
  "reason": "Risk is too high without rollback validation.",
  "idempotency_key": "client-generated-uuid"
}
```

Approval state machine:

```text
pending -> approved
pending -> rejected
pending -> expired
pending -> cancelled
```

Terminal states are read-only. Repeated approve/reject on a terminal request
returns `409` or `410` with the current approval state. `reject.reason` is
required.

Page states:

- Pending empty: no approval requests assigned to me.
- History empty: no approvals in selected scope/time range.
- Unauthorized: user has no approval role or no matching scope.
- Expired/conflict: refresh detail, disable action buttons, show terminal state.
- Error: show retry and request id.

### AIO-92: Cost Overview and Cost Panels

Routes:

- `/costs`
- incident detail cost panel on `/incidents/:incident_id`

Primary APIs:

```http
GET /api/costs/summary
GET /api/costs/breakdown
GET /api/incidents/{incident_id}/costs
GET /api/sessions/{session_id}/costs
```

Filters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `time_from`, `time_to` | RFC3339 | Required for overview. |
| `group_by` | csv enum | `team,service,incident,session,tool,model,day`. |
| `team_id` | string | Scope filter. |
| `service_id` | string | Scope filter. |
| `incident_id` | string | Drill-down filter. |
| `session_id` | string | Drill-down filter. |
| `cost_type` | csv enum | `llm,tool,k8s,diagnosis_duration`. |
| `page`, `page_size`, `sort` | mixed | Default sort `-estimated_cost_usd`. |

Summary fields:

```json
{
  "time_range": {
    "from": "2026-06-01T00:00:00Z",
    "to": "2026-06-12T23:59:59Z"
  },
  "totals": {
    "estimated_cost_usd": 123.45,
    "llm_calls": 312,
    "input_tokens": 420000,
    "output_tokens": 93000,
    "tool_calls": 860,
    "mcp_calls": 510,
    "k8s_read_executions": 128,
    "diagnosis_duration_ms": 932000
  },
  "currency": "USD",
  "estimation_note": "Estimated from Gateway/control-plane usage records."
}
```

Breakdown item fields:

```json
{
  "group": {
    "kind": "service",
    "id": "svc_payment_api",
    "name": "payment-api"
  },
  "estimated_cost_usd": 12.34,
  "llm_calls": 28,
  "input_tokens": 41000,
  "output_tokens": 9000,
  "tool_calls": 75,
  "mcp_calls": 41,
  "k8s_read_executions": 12,
  "diagnosis_duration_ms": 83000,
  "incident_count": 6,
  "session_count": 8
}
```

Cost source rules:

- Gateway/control-plane aggregates and normalizes all cost data.
- Frontend displays estimates; it does not compute authoritative billing.
- Cost is for AIOps diagnostic operations only, not full cloud FinOps.
- Missing cost records should render as unavailable, not zero.

Page states:

- Loading: summary and table/chart skeletons.
- Empty: no cost records for selected range/scope.
- Partial: some sessions have missing cost fields; show estimate caveat.
- Unauthorized: no cost permission for selected scope.
- Error: retryable state with request id.

### Management Entry Points

V1 includes only minimal validation entry points needed by incidents and
notifications. Full configuration management is later.

APIs:

```http
GET /api/services
GET /api/teams
GET /api/ownership/resolve?service=...&namespace=...&workload=...
GET /api/notification-channels
```

Required fields:

- Service: `service_id`, `service_name`, `owner_team_id`, `source`, `confidence`,
  `updated_at`, `unowned`.
- Team: `team_id`, `team_name`, `default_channel_id`, `oncall_policy_ref`.
- Notification channel: `channel_id`, `team_id`, `type=feishu`, `display_name`,
  `enabled`, `delivery_health`.

Admin-only write APIs may be added later, but AIO-88 through AIO-92 should not
depend on full configuration writes.

## Global Page State Specification

Every page and panel should support these states independently:

| State | Requirement |
| --- | --- |
| Loading | Preserve layout dimensions; disable actions; do not show stale counts as current. |
| Empty | Explain what is absent in domain terms; keep relevant filters/actions visible. |
| Error | Show retry control and `request_id`; avoid raw stack traces. |
| Unauthorized | Distinguish unauthenticated `401` from forbidden `403`; do not leak hidden resource counts. |
| Partial | Render available panels and label missing evidence/cost/Grafana data explicitly. |
| Stale/conflict | Disable mutating actions, refresh authoritative resource, show current terminal state. |

Panel-level failures must not blank the full incident detail page. For example,
Grafana failure should not hide diagnosis, and cost unavailability should not
hide evidence.

## Backend Readiness Dependencies

Frontend implementation should start only after these minimum backend contracts
exist or are stubbed with contract tests:

| Dependency | Issue | Required for |
| --- | --- | --- |
| Internal Approval Service API | AIO-80 | AIO-91 and approval panels in AIO-89. |
| LDAP/RBAC | AIO-84 | All pages and action gating. |
| CMDB ownership and team routing | AIO-85 | Incident filters, unowned/default-team flags, approval scope, notification routing. |
| Notification Center and Feishu notification-only | AIO-86 | Timeline notification events and approval reminder/result visibility. |
| Diagnosis artifact writeback | AIO-95 | Durable incident detail beyond P0 HTTP export. |
| Topology runtime and evidence precision | AIO-93, AIO-94, AIO-96 | Better evidence completeness and root-cause specificity; not required for page skeleton contract if partial states are implemented. |

## Implementation Split Recommendation

1. AIO-88 should implement layout shell, auth-aware navigation, incident list
   data adapter, filters, pagination, and empty/error/unauthorized states.
2. AIO-89 should implement incident detail with independent panels for header,
   timeline, diagnosis, evidence, approval summary, Grafana placeholder, audit,
   and cost summary.
3. AIO-90 should implement only Gateway-provided Grafana panel rendering and
   fallback modes. It should not create Grafana credentials or direct queries.
4. AIO-91 should implement Approval Center after AIO-80 API shape stabilizes.
   The UI should trust `can_approve`, `can_reject`, and `blocked_reason` for
   display, while treating Gateway response codes as authoritative.
5. AIO-92 should implement cost overview and drill-down after Gateway exposes
   normalized usage records. Missing cost should be `unavailable`, not zero.

Recommended frontend adapter boundary:

```text
src/api/gatewayClient.ts
src/api/incidents.ts
src/api/approvals.ts
src/api/costs.ts
src/api/grafana.ts
```

No adapter should import Hermes, Connector, MCP, Feishu approval, Prometheus, or
Loki clients.

## Open Risks

- Legacy code still contains Feishu-native approval compatibility paths.
  Console V1 must follow the internal Approval Service decision in this
  document and AIO-80.
- P0 accepted Hermes HTTP export as the smoke artifact of record. Durable
  diagnosis writeback is still a backend dependency for production history.
- Topology evidence may be skipped and K8s selectors may be imprecise until the
  follow-up issues complete. The UI must render partial evidence honestly.
- RBAC and CMDB contracts are in progress. Field names in this document should
  be treated as the handoff target and may need minor adjustment during AIO-84
  and AIO-85 implementation.
