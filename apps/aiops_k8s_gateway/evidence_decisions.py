"""Gateway-owned Evidence Steps, judgments, and Recommended Actions."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3

from .evidence_decision_schema import MIGRATIONS
from .gateway_db import register_migrations
JSON = dict[str, object]
register_migrations(MIGRATIONS)

_STATES = {"running", "succeeded", "partial", "failed", "skipped"}
_LEGACY_EVIDENCE_TTL_SECONDS = 300
_OPTIONAL_SOURCES = {"topology"}
_SOURCES = {
    "query_metrics": "prometheus",
    "metrics": "prometheus",
    "query_logs": "loki",
    "logs": "loki",
    "run_k8s_read": "k8s",
    "k8s_read": "k8s",
    "get_service_topology": "topology",
}


class EvidenceDecisionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def record_diagnosis_facts(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    investigation_id: str,
    payload: JSON,
    created_at: float,
) -> JSON:
    """Canonicalize one accepted Diagnosis result inside its Gateway transaction."""
    target = _target(conn, investigation_id)
    legacy_steps = payload.get("evidence_steps") is None
    steps = _steps(payload, request_id=request_id, target=target, created_at=created_at)
    diagnosis = payload["diagnosis"]
    assert isinstance(diagnosis, dict)
    actions = _actions(
        diagnosis,
        request_id=request_id,
        steps=steps,
        target=target,
        created_at=created_at,
        allow_legacy_step_aliases=legacy_steps,
    )
    guidance = _texts(diagnosis.get("next_verification", []), "diagnosis.next_verification")
    missing_evidence = payload.get("missing_evidence", [])
    if not isinstance(missing_evidence, list) or any(not isinstance(item, dict) for item in missing_evidence):
        raise EvidenceDecisionError("invalid_result", "missing_evidence must be a list of objects")
    if len(missing_evidence) > 100:
        raise EvidenceDecisionError("invalid_result", "missing_evidence must not exceed 100 items")
    for item in missing_evidence:
        assert isinstance(item, dict)
        missing = _text(
            item.get("reason") or item.get("source_type") or "Collect missing evidence",
            "missing_evidence.reason",
        )
        if missing not in guidance:
            guidance.append(missing)
    for action in actions:
        guidance.extend(str(reason) for reason in action["gate"]["reasons"] if str(reason) not in guidance)  # type: ignore[index,union-attr]
    gate_status = "incomplete" if any(action["gate"]["status"] == "incomplete" for action in actions) else "complete"  # type: ignore[index]
    required_steps = [step for step in steps if step["source"] not in _OPTIONAL_SOURCES]
    required_missing = [item for item in missing_evidence if not _optional_missing_evidence(item)]
    if required_missing or not required_steps or any(step["state"] != "succeeded" for step in required_steps):
        gate_status = "incomplete"
    summary = _text(diagnosis.get("summary"), "diagnosis.summary")

    for step in steps:
        conn.execute(
            """
            INSERT INTO evidence_steps (
                id, investigation_id, sequence, purpose, source, scope_json, state, result,
                impact, evidence_references_json, missing_guidance, observed_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step["id"], investigation_id, step["sequence"], step["purpose"], step["source"],
                _canonical(step["scope"]), step["state"], step["result"], step["impact"],
                _canonical(step["evidence_references"]), step["missing_guidance"],
                step["observed_at"], step["expires_at"],
            ),
        )
    conn.execute(
        "INSERT INTO investigation_judgments (investigation_id, summary, evidence_gate_status, next_evidence_guidance_json) VALUES (?, ?, ?, ?)",
        (investigation_id, summary, gate_status, _canonical(guidance)),
    )
    for action in actions:
        conn.execute(
            """
            INSERT INTO recommended_actions (
                id, investigation_id, version, summary, change_intent, target_json,
                evidence_step_ids_json, safeguards_json, gate_status,
                gate_reasons_json, action_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action["id"], investigation_id, action["version"], action["summary"],
                action["change_intent"],
                _canonical(action["target"]),
                _canonical(action["evidence_step_ids"]), _canonical(action["safeguards"]),
                action["gate"]["status"],  # type: ignore[index]
                _canonical(action["gate"]["reasons"]), action["hash"], created_at,  # type: ignore[index]
            ),
        )
    return {"evidence_steps": steps, "recommended_actions": actions, "judgment": _judgment(summary, gate_status, guidance)}


def project(conn: sqlite3.Connection, investigation_id: str | None, *, now: float) -> JSON:
    if not investigation_id:
        return {"evidence_steps": [], "judgment": None, "recommended_actions": []}
    steps = [_step(row) for row in conn.execute(
        "SELECT * FROM evidence_steps WHERE investigation_id = ? ORDER BY sequence", (investigation_id,)
    )]
    judgment_row = conn.execute(
        "SELECT * FROM investigation_judgments WHERE investigation_id = ?", (investigation_id,)
    ).fetchone()
    step_expiry = {str(step["id"]): float(step["expires_at"]) for step in steps}
    current_target = _target(conn, investigation_id)
    actions = []
    for row in conn.execute(
        "SELECT * FROM recommended_actions WHERE investigation_id = ? ORDER BY version, id", (investigation_id,)
    ):
        step_ids = json.loads(str(row["evidence_step_ids_json"]))
        expires_at = min((step_expiry.get(str(step_id), 0) for step_id in step_ids), default=0)
        expired = expires_at < now
        target_changed = json.loads(str(row["target_json"])) != current_target
        actions.append(_action(row, expired=expired, target_changed=target_changed, expires_at=expires_at))
    return {
        "evidence_steps": steps,
        "judgment": _judgment_row(judgment_row) if judgment_row is not None else None,
        "recommended_actions": actions,
    }


def invalidate_decisions(conn: sqlite3.Connection, investigation_id: str, action_ids: list[str]) -> None:
    conn.execute("UPDATE investigation_judgments SET valid = 0 WHERE investigation_id = ?", (investigation_id,))
    if action_ids:
        placeholders = ",".join("?" for _ in action_ids)
        conn.execute(
            f"UPDATE recommended_actions SET stale = 1 WHERE investigation_id = ? AND id IN ({placeholders})",
            (investigation_id, *action_ids),
        )


def stale_incident_actions(conn: sqlite3.Connection, incident_id: str) -> None:
    conn.execute(
        """
        UPDATE recommended_actions SET stale = 1
        WHERE stale = 0 AND investigation_id IN (
            SELECT id FROM investigations WHERE incident_id = ?
        )
        """,
        (incident_id,),
    )


def _target(conn: sqlite3.Connection, investigation_id: str) -> JSON:
    row = conn.execute(
        """
        SELECT inc.cluster_id, inc.namespace, inc.workload_kind, inc.workload_name,
               inc.deployment_target_id, inc.resource_binding_id,
               COALESCE(rb.revision, inc.binding_revision) AS binding_revision
        FROM investigations i
        JOIN incidents inc ON inc.id = i.incident_id
        LEFT JOIN resource_bindings rb ON rb.id = inc.resource_binding_id
        WHERE i.id = ?
        """,
        (investigation_id,),
    ).fetchone()
    if row is None:
        raise EvidenceDecisionError("investigation_not_found", "Investigation was not found")
    return {
        "cluster_id": str(row["cluster_id"]),
        "namespace": str(row["namespace"]),
        "workload_kind": row["workload_kind"],
        "workload_name": row["workload_name"],
        "deployment_target_id": row["deployment_target_id"],
        "resource_binding_id": row["resource_binding_id"],
        "binding_revision": row["binding_revision"],
    }


def _steps(payload: JSON, *, request_id: str, target: JSON, created_at: float) -> list[JSON]:
    submitted = payload.get("evidence_steps")
    if submitted is None:
        submitted = [_legacy_step(item, index, request_id, target, created_at) for index, item in enumerate(payload.get("steps", []), 1)]
    if not isinstance(submitted, list) or any(not isinstance(item, dict) for item in submitted):
        raise EvidenceDecisionError("invalid_evidence_step", "evidence_steps must be a list of objects")
    if len(submitted) > 100:
        raise EvidenceDecisionError("invalid_evidence_step", "evidence_steps must not exceed 100 items")
    result = [_canonical_step(item, index) for index, item in enumerate(submitted, 1)]
    ids = [step["id"] for step in result]
    if len(set(ids)) != len(ids):
        raise EvidenceDecisionError("invalid_evidence_step", "Evidence Step IDs must be unique")
    return result


def _canonical_step(item: JSON, sequence: int) -> JSON:
    state = _text(item.get("state"), "evidence_steps.state")
    if state not in _STATES:
        raise EvidenceDecisionError("invalid_evidence_step", "unsupported Evidence Step state")
    scope = item.get("scope")
    if not isinstance(scope, dict):
        raise EvidenceDecisionError("invalid_evidence_step", "Evidence Step scope must be an object")
    canonical_scope = {
        "cluster_id": _text(scope.get("cluster_id"), "evidence_steps.scope.cluster_id"),
        "namespace": _text(scope.get("namespace"), "evidence_steps.scope.namespace"),
        "workload_kind": _optional_text(scope.get("workload_kind"), "evidence_steps.scope.workload_kind"),
        "workload_name": _optional_text(scope.get("workload_name"), "evidence_steps.scope.workload_name"),
    }
    result = _optional_text(item.get("result"), "evidence_steps.result")
    missing = _optional_text(item.get("missing_guidance"), "evidence_steps.missing_guidance")
    if state == "succeeded" and result is None:
        raise EvidenceDecisionError("invalid_evidence_step", "succeeded Evidence Step requires a result")
    if state != "succeeded" and result is None and missing is None:
        raise EvidenceDecisionError("invalid_evidence_step", "incomplete Evidence Step requires a result or missing guidance")
    if state in {"partial", "failed", "skipped"} and missing is None:
        raise EvidenceDecisionError("invalid_evidence_step", "partial, failed, or skipped Evidence Step requires missing guidance")
    observed_at = _number(item.get("observed_at"), "evidence_steps.observed_at")
    expires_at = _number(item.get("expires_at"), "evidence_steps.expires_at")
    if expires_at < observed_at:
        raise EvidenceDecisionError("invalid_evidence_step", "Evidence Step expires_at must not precede observed_at")
    return {
        "id": _text(item.get("id"), "evidence_steps.id"),
        "sequence": sequence,
        "purpose": _text(item.get("purpose"), "evidence_steps.purpose"),
        "source": _text(item.get("source"), "evidence_steps.source"),
        "scope": canonical_scope,
        "state": state,
        "result": result,
        "impact": _text(item.get("impact"), "evidence_steps.impact"),
        "evidence_references": _texts(item.get("evidence_references", []), "evidence_steps.evidence_references"),
        "missing_guidance": missing,
        "observed_at": observed_at,
        "expires_at": expires_at,
    }


def _legacy_step(item: object, index: int, request_id: str, target: JSON, created_at: float) -> JSON:
    if not isinstance(item, dict):
        raise EvidenceDecisionError("invalid_evidence_step", "steps must contain objects")
    source = _SOURCES.get(str(item.get("source_type") or item.get("tool")), str(item.get("source_type") or item.get("tool") or "diagnosis"))
    state = str(item.get("status") or "failed")
    state = state if state in _STATES else "failed"
    summary = str(item.get("summary") or "Evidence collection did not return a result")
    reference = item.get("evidence_ref")
    if isinstance(reference, dict):
        reference = reference.get("ref_id") or reference.get("source_ref")
    missing = str(item.get("missing_reason") or summary) if state != "succeeded" else None
    return {
        "id": f"{request_id}:step:{index}",
        "purpose": f"Collect {source} evidence",
        "source": source,
        "scope": {field: target[field] for field in ("cluster_id", "namespace", "workload_kind", "workload_name")},
        "state": state,
        "result": summary,
        "impact": summary,
        "evidence_references": [str(reference)] if reference else [],
        "missing_guidance": missing,
        "observed_at": created_at,
        "expires_at": created_at + _LEGACY_EVIDENCE_TTL_SECONDS,
    }


def _actions(
    diagnosis: JSON,
    *,
    request_id: str,
    steps: list[JSON],
    target: JSON,
    created_at: float,
    allow_legacy_step_aliases: bool = False,
) -> list[JSON]:
    submitted = diagnosis.get("recommended_actions", [])
    if not isinstance(submitted, list) or any(not isinstance(item, dict) for item in submitted):
        raise EvidenceDecisionError("invalid_recommended_action", "recommended_actions must be a list of objects")
    if len(submitted) > 50:
        raise EvidenceDecisionError("invalid_recommended_action", "recommended_actions must not exceed 50 items")
    step_by_id = {str(step["id"]): step for step in steps}
    result = []
    for index, item in enumerate(submitted, 1):
        action_id = _text(
            item.get("id") or item.get("action_proposal_id") or f"{request_id}:action:{index}",
            "recommended_actions.id",
        )
        forbidden = set(item) & {
            "action_type", "parameters", "rollback_plan", "approval_required",
            "execute_automatically",
        }
        if forbidden:
            raise EvidenceDecisionError(
                "invalid_recommended_action",
                "Recommended Actions are guidance and cannot contain executable fields",
            )
        safeguards = _texts(item.get("safeguards", []), "recommended_actions.safeguards")
        step_ids = _texts(item.get("evidence_step_ids", list(step_by_id)), "recommended_actions.evidence_step_ids")
        if allow_legacy_step_aliases:
            aliases = {f"step-{step['sequence']}": str(step["id"]) for step in steps}
            step_ids = [aliases.get(step_id, step_id) for step_id in step_ids]
            if len(set(step_ids)) != len(step_ids):
                raise EvidenceDecisionError(
                    "invalid_recommended_action",
                    "recommended_actions.evidence_step_ids must resolve to unique Evidence Steps",
                )
            if step_ids and all(step_id in step_by_id for step_id in step_ids):
                step_ids = [
                    str(step["id"])
                    for step in steps
                    if step["state"] == "succeeded" and step["source"] not in _OPTIONAL_SOURCES
                ]
        reasons = _gate_reasons(
            item, safeguards, step_ids, step_by_id, target, created_at
        )
        summary = _text(item.get("summary"), "recommended_actions.summary")
        change_intent = item.get("change_intent", "generic")
        if change_intent not in {"generic", "controlled_restart"}:
            raise EvidenceDecisionError(
                "invalid_recommended_action", "unsupported Recommended Action change_intent",
            )
        frozen = {
            "summary": summary,
            "change_intent": change_intent,
            "target": target,
            "evidence_step_ids": step_ids,
            "safeguards": safeguards,
        }
        result.append(
            {
                "id": action_id,
                "version": 1,
                "summary": summary,
                "change_intent": change_intent,
                "target": target,
                "evidence_step_ids": step_ids,
                "safeguards": safeguards,
                "gate": {"status": "incomplete" if reasons else "complete", "reasons": reasons},
                "hash": hashlib.sha256(_canonical(frozen).encode()).hexdigest(),
                "stale": False,
            }
        )
    ids = [action["id"] for action in result]
    if len(set(ids)) != len(ids):
        raise EvidenceDecisionError("invalid_recommended_action", "Recommended Action IDs must be unique")
    return result


def _gate_reasons(
    submitted: JSON,
    safeguards: list[str],
    step_ids: list[str],
    steps: dict[str, JSON],
    target: JSON,
    now: float,
) -> list[str]:
    reasons = []
    if target["deployment_target_id"] is None or target["resource_binding_id"] is None:
        reasons.append("target requires a confirmed Resource Binding")
    submitted_target = submitted.get("target")
    if submitted_target is not None and submitted_target != target:
        reasons.append("submitted target does not match the Incident Deployment Target")
    if not safeguards:
        reasons.append("at least one safeguard is required")
    if not step_ids:
        reasons.append("action must reference Evidence Steps")
    referenced = [steps.get(step_id) for step_id in step_ids]
    if any(step is None for step in referenced):
        reasons.append("action references an unknown Evidence Step")
    valid_steps = [step for step in referenced if step is not None]
    if any(step["state"] != "succeeded" for step in valid_steps):
        reasons.append("all referenced Evidence Steps must succeed")
    if any(float(step["expires_at"]) < now for step in valid_steps):
        reasons.append("referenced evidence is stale")
    expected_scope = {field: target[field] for field in ("cluster_id", "namespace", "workload_kind", "workload_name")}
    if any(step["scope"] != expected_scope for step in valid_steps):
        reasons.append("referenced evidence is outside the action scope")
    if any(not step["evidence_references"] for step in valid_steps):
        reasons.append("referenced Evidence Step has no evidence reference")
    return reasons


def _optional_missing_evidence(item: JSON) -> bool:
    source = str(item.get("source_type") or item.get("tool") or "")
    return _SOURCES.get(source, source) in _OPTIONAL_SOURCES


def _step(row: sqlite3.Row) -> JSON:
    return {
        "id": str(row["id"]), "sequence": int(row["sequence"]), "purpose": str(row["purpose"]),
        "source": str(row["source"]), "scope": json.loads(str(row["scope_json"])), "state": str(row["state"]),
        "result": row["result"], "impact": str(row["impact"]),
        "evidence_references": json.loads(str(row["evidence_references_json"])),
        "missing_guidance": row["missing_guidance"], "observed_at": float(row["observed_at"]),
        "expires_at": float(row["expires_at"]),
    }


def _action(row: sqlite3.Row, *, expired: bool, target_changed: bool, expires_at: float) -> JSON:
    reasons = json.loads(str(row["gate_reasons_json"]))
    stale = bool(row["stale"]) or expired or target_changed
    if bool(row["stale"]):
        reasons = [*reasons, "action is stale"]
    if expired and "referenced evidence is stale" not in reasons:
        reasons = [*reasons, "referenced evidence is stale"]
    if target_changed:
        reasons = [*reasons, "action target no longer matches the current Resource Binding"]
    return {
        "id": str(row["id"]), "version": int(row["version"]),
        "summary": str(row["summary"]), "change_intent": str(row["change_intent"]),
        "target": json.loads(str(row["target_json"])),
        "evidence_step_ids": json.loads(str(row["evidence_step_ids_json"])),
        "safeguards": json.loads(str(row["safeguards_json"])),
        "gate": {"status": "incomplete" if stale else str(row["gate_status"]), "reasons": reasons},
        "hash": str(row["action_hash"]), "stale": stale,
        "expires_at": expires_at,
    }


def _judgment(summary: str, gate_status: str, guidance: list[str]) -> JSON:
    return {"summary": summary, "valid": True, "evidence_gate_status": gate_status, "next_evidence_guidance": guidance}


def _judgment_row(row: sqlite3.Row) -> JSON:
    return {
        "summary": str(row["summary"]), "valid": bool(row["valid"]),
        "evidence_gate_status": str(row["evidence_gate_status"]),
        "next_evidence_guidance": json.loads(str(row["next_evidence_guidance_json"])),
    }


def _text(value: object, field: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result or len(result) > 4000:
        raise EvidenceDecisionError("invalid_result", f"{field} must be a non-empty string up to 4000 characters")
    return result


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _texts(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise EvidenceDecisionError("invalid_result", f"{field} must be a list")
    if len(value) > 100:
        raise EvidenceDecisionError("invalid_result", f"{field} must not exceed 100 items")
    result = [_text(item, field) for item in value]
    if len(set(result)) != len(result):
        raise EvidenceDecisionError("invalid_result", f"{field} must not contain duplicates")
    return result


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise EvidenceDecisionError("invalid_result", f"{field} must be a finite number")
    return float(value)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
