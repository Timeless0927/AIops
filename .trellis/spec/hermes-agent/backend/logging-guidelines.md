# Logging & Audit

> The durable observability channel in this project is **the `audit_log` SQLite
> store**, written via `toolsets/audit_log.record_audit`. There is no app-wide
> structured-log pipeline, and stdlib `logging` is only a **defensive fallback**
> for a handful of worker/extractor files. Access logs are **suppressed**.

---

## What actually runs

### The durable channel: `toolsets/audit_log.py` (SQLite WAL)

`record_audit(...)` (`toolsets/audit_log.py:340`) writes one row to the `audit_log`
table with stable fields: `who, what, when_ts, cluster, namespace, trigger,
tool_level, tool_name, dry_run, result, approval_by, approval_at, rollback,
snapshot_path, incident_id, actor, role, scope, request_id, permission, decision,
resource_scope, approval_id, action_proposal_id`.

Gateway wrappers (`apps/aiops_k8s_gateway/main.py:138`):

```python
def _record_gateway_audit(actor, *, request_id, action, result, cluster=None,
        namespace=None, incident_id=None, permission=None, decision=None,
        resource_scope=None, approval_id=None, action_proposal_id=None):
    asyncio.run(audit_log.record_audit(
        who=actor.username, what=action,
        cluster=cluster, namespace=namespace,
        trigger="gateway", tool_level="control-plane", tool_name="gateway",
        result=result, incident_id=incident_id, actor=actor.actor_id,
        role=_audit_role(actor), scope=actor.scope.to_dict(), request_id=request_id,
        permission=permission, decision=decision,
        resource_scope=resource_scope.to_dict() if resource_scope else None,
        approval_id=approval_id, action_proposal_id=action_proposal_id))
```

The Gateway also records **denials** via `_record_gateway_authz_audit` (line 177):
`what="gateway_authorize"`, `result` `unauthorized`|`forbidden`. A minority of
approval-scoped denials use `what="approval_authorize"` (line 962).

### The incident timeline: `toolsets/incident_store.py` event rows

Per-incident step history goes to `incident_events`, not `audit_log`
(`incident_store.add_event`, line 445). `event_type` must be in
`_VALID_EVENT_TYPES` (line 91). Timeline is the human-readable per-incident trail;
audit_log is the cross-cutting control-plane trail. Hermes contributes timeline
events via `_record_timeline_event` (`hermes/service_main.py:322`).

### `request_id` / `correlation_id` propagation

- `request_id` is generated/read by `_request_id` (`apps/aiops_k8s_gateway/main.py:52`)
  from `X-Request-ID` / `X-Correlation-ID`, else `req-{uuid4().hex}`.
- It is threaded into both the HTTP response (`_error_payload`, success payloads) and
  every `audit_log.record_audit` / timeline `_metadata` field.
- MCP envelopes carry `request_id` and `correlation_id` inside `ToolEnvelope`
  (`aiops/contracts/envelope.py:12`); Hermes preserves it in
  `_correlation_id` (`hermes/service_main.py:643`).

---

## stdlib `logging` — defensive fallback only

`import logging` / `getLogger` appears in a small set of files as a **last-resort**
log for failure paths, never as the primary observability for a request:

| file | use |
|------|-----|
| `toolsets/loki_query.py`, `toolsets/prometheus_query.py` | `logger.warning` on audit-write failure and slow queries |
| `toolsets/sre_extractor.py` | `logger.warning` on AI extractor exception |
| `runtime/hermes_gateway.py`, `runtime/approval_execution_worker.py`, `runtime/feishu_approval_overlay.py` | `logger.exception`/`logger.warning` on worker bootstrap or callback failures |

Pattern when you genuinely need it — module-level `logger = logging.getLogger(__name__)`,
emit `logger.warning`/`logger.exception("...", exc_info=...)` for an unexpected
path that has no durable store. Do **not** replace `audit_log` with `logging.info`.

`print()` appears *only* in pre-deploy smoke CLIs (`runtime/image_smoke.py`,
`runtime/service_image_smoke.py`, `runtime/service_mesh_smoke.py`). Do not use
`print` in a service.

---

## What to log, what not to

**Do** write to `audit_log`:
- Every authorization decision: allow *and* deny (with `permission`/`decision`).
- Every state-changing Gateway action: `approval_create`, `approval_<action>`,
  `case_profile_backfill`, `k8s_read`, `incident_query`, `audit_query`,
  `ldap_login`, `ldap_sync`.
- Connector read execution (audit context attached to the response,
  `apps/aiops_k8s_gateway/main.py:611`).

**Do not**:
- Re-enable `JsonHandler.log_message` — access logs stay off. Durable events
  carry their own audit/timeline rows.
- Log raw values from `Authorization`, the writeback HMAC secret
  (`AIOPS_GATEWAY_WRITEBACK_SECRET`), session tokens, or LDAP bind passwords.
  Audit records carry `actor_id`/`username`, never the token itself.
- Duplicate a durable event into `logging` "just in case". Pick the channel.

---

## Common mistakes

- Adding a new Gateway action and forgetting `_record_gateway_audit` for success
  *and* the deny path. Audit completeness matters more than log noise.
- Letting `toolsets/k8s_*` or evidence collectors write secrets to stdout/print —
  k8s read output is already redacted for known secret patterns
  (`tests/test_command_gateway_skeleton.py:382`). New redaction patterns belong
  with the collector, not in the audit row.
- Using `logging` as the primary record for a request when the same fact belongs in
  `audit_log`/`incident_events`.

---

## AIOps services must emit lifecycle lines to stdout (Loki collection surface)

> **Why**: in `dev-external`, Loki evidence is collected by alloy scraping pod
> **stdout**. The ADR-0005 Issue A evidence smoke asserts that Loki returns
> non-empty `line_count` for the alert's target namespace. If an AIOps service
> pod emits nothing to stdout, alloy has nothing to scrape and the logs channel
> is structurally empty (`line_count:0`) regardless of how correct the overlay
> or backend wiring is. Deploy cannot fix this; the service must.
> See `deploy/dev-external-observability-contract.md` §5 Bad case 1.

Every runnable AIOps service (`aiops-gateway`, `aiops-connector`, `aiops-hermes`,
`aiops-mcp-prometheus`, `aiops-mcp-loki`, `aiops-mcp-topology`) must emit, at
minimum, one stdout line per request/diagnosis lifecycle transition
(handoff received, adapter invoked, observation status, session end). This is
**in addition to** the `audit_log`/`incident_events` durable rows — those are
queried by id from SQLite, not by alloy. Alloy only sees stdout.

Verification: `kubectl -n aiops-dev logs deploy/aiops-<svc>` must be non-empty
after exercising the service. Empty logs ⇒ the Loki evidence channel will be
`line_count:0` in any `dev-external` smoke.

This is the **collection surface contract**, distinct from the durable-channel
rules above: `audit_log` is for replay-by-id, stdout is for alloy/Loki
collection. Both are required; neither substitutes for the other.

**Where it is implemented**: `apps/service_http.py` `JsonHandler.log_request`
emits one stdout access line per request for all six services (they all subclass
`JsonHandler`). `log_message` stays a silent `return` so incidental `log_error`
does not add alloy noise. A service whose stdout is empty despite traffic means
this base hook is bypassed — check that the handler still traces back to
`JsonHandler`, or that the pod did not take a stale image (configmap changes do
not auto-roll Deployments; see `deploy/dev-external-observability-contract.md`
§Rollout note).
