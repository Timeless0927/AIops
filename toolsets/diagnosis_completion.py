"""Deterministic completion policy for the governed Diagnosis loop."""

from __future__ import annotations

import json
from typing import Any, Iterable

from toolsets.recommendations import normalize_recommendations


_RELATIONS = frozenset({"supports", "refutes", "uncertain"})
_SCOPE_FIELDS = ("cluster_id", "namespace", "service", "workload_kind", "workload_name")


class ModelResponseError(ValueError):
    code = "invalid_response"
    no_retry = True


def diagnosis_from_llm(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ModelResponseError("empty assistant final content")
    try:
        parsed = json.loads(_extract_json_object(content.strip()))
    except json.JSONDecodeError as exc:
        raise ModelResponseError("assistant final content was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError("assistant final JSON was not an object")
    _validate_diagnosis_fields(parsed)
    return parsed


def _validate_diagnosis_fields(payload: dict[str, Any]) -> None:
    candidates = payload.get("root_cause_candidates")
    actions = payload.get("recommended_actions", [])
    confidence = payload.get("confidence")
    if not isinstance(candidates, list) or not candidates:
        raise ModelResponseError("root_cause_candidates must be a non-empty list")
    for candidate in candidates:
        score = candidate.get("confidence") if isinstance(candidate, dict) else None
        if (
            not isinstance(candidate, dict)
            or not isinstance(candidate.get("cause"), str)
            or not str(candidate["cause"]).strip()
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= float(score) <= 1
        ):
            raise ModelResponseError("root cause candidate fields are invalid")
        refs = candidate.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ModelResponseError("root cause evidence_refs must contain strings")
    if not isinstance(actions, list) or any(
        not isinstance(action, dict)
        or not isinstance(action.get("summary"), str)
        or not str(action["summary"]).strip()
        for action in actions
    ):
        raise ModelResponseError("recommended_actions fields are invalid")
    if not isinstance(confidence, dict):
        raise ModelResponseError("confidence must be an object")
    score = confidence.get("score")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not 0 <= float(score) <= 1
        or confidence.get("level") not in {"high", "medium", "low"}
    ):
        raise ModelResponseError("confidence fields are invalid")


def _extract_json_object(content: str) -> str:
    payload = content.strip()
    if payload.startswith("```"):
        lines = payload.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        payload = "\n".join(lines).strip()
    if payload.startswith("{"):
        return payload
    start = payload.find("{")
    if start < 0:
        return payload
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(payload[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return payload[start:index + 1]
    return payload[start:]


def initial_hypothesis_state(incident: dict[str, Any]) -> dict[str, Any]:
    goal = str(incident.get("summary") or incident.get("alert_name") or "定位 Incident 根因")
    return {
        "goal": goal,
        "candidates": [
            {
                "cause": "待通过 Investigation Evidence 验证的候选原因",
                "evidence_relations": [],
                "unknowns": [],
                "next_checks": [],
            }
        ],
    }


def record_observation(state: dict[str, Any], observation: dict[str, Any]) -> None:
    """Make every accepted Observation affect the current candidate state."""
    ref = _observation_ref(observation)
    if not ref:
        return
    for candidate in state.get("candidates", []):
        relations = candidate.setdefault("evidence_relations", [])
        if not any(item.get("evidence_ref") == ref for item in relations if isinstance(item, dict)):
            relations.append({"evidence_ref": ref, "relation": "uncertain"})
    if observation.get("status") not in {"succeeded", "partial"}:
        reason = str(observation.get("missing_reason") or observation.get("summary") or "证据不可用")
        for candidate in state.get("candidates", []):
            unknowns = candidate.setdefault("unknowns", [])
            if reason not in unknowns:
                unknowns.append(reason)


def apply_turn_hypotheses(
    state: dict[str, Any], content: Any, observations: list[dict[str, Any]]
) -> None:
    """Accept only the structured, user-visible part of a tool-turn update."""
    if not isinstance(content, str) or not content.strip():
        return
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return
    hypothesis = payload.get("hypothesis_state") if isinstance(payload, dict) else None
    raw_candidates = hypothesis.get("candidates") if isinstance(hypothesis, dict) else None
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return
    observation_aliases = {
        _observation_ref(observation): _observation_refs(observation)
        for observation in observations
        if _observation_ref(observation)
    }
    allowed_refs = {ref for aliases in observation_aliases.values() for ref in aliases}
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates[:8]:
        if not isinstance(raw, dict) or not isinstance(raw.get("cause"), str):
            continue
        cause = raw["cause"].strip()[:1000]
        if not cause:
            continue
        relations, _issues = _candidate_relations(raw, allowed_refs)
        related = {item["evidence_ref"] for item in relations}
        related_observations = {
            primary
            for primary, aliases in observation_aliases.items()
            if related & aliases
        }
        relations.extend(
            {"evidence_ref": ref, "relation": "uncertain"}
            for ref in sorted(set(observation_aliases) - related_observations)
        )
        candidates.append(
            {
                "cause": cause,
                "evidence_relations": relations,
                "unknowns": _texts(raw.get("unknowns")),
                "next_checks": _texts(raw.get("next_checks")),
            }
        )
    if candidates:
        state["candidates"] = candidates


def record_unavailable_sources(
    missing_evidence: list[dict[str, Any]],
    adapters: dict[str, Any],
    tool_sources: dict[str, str],
) -> None:
    known = {str(item.get("source_type") or "") for item in missing_evidence}
    for tool, source in tool_sources.items():
        if adapters.get(tool) is not None or source in known:
            continue
        missing_evidence.append(
            {
                "source_type": source,
                "tool": tool,
                "reason": f"required {source} adapter is unavailable",
                "audit": {"reason_code": "required_source_unavailable"},
            }
        )


def validate_completion(
    payload: dict[str, Any],
    *,
    incident: dict[str, Any],
    observations: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    required_sources: Iterable[str],
    remaining_evidence_steps: int,
) -> dict[str, Any]:
    issues: list[str] = []
    observation_aliases = {
        _observation_ref(observation): _observation_refs(observation)
        for observation in observations
        if _observation_ref(observation)
    }
    allowed_refs = {
        ref
        for aliases in observation_aliases.values()
        for ref in aliases
    }
    accepted_observation_aliases = {
        primary: aliases
        for primary, aliases in observation_aliases.items()
        if any(
            _observation_ref(observation) == primary
            and observation.get("status") in {"succeeded", "partial"}
            and observation.get("evidence_ref")
            for observation in observations
        )
    }
    accepted_refs = {
        ref
        for aliases in accepted_observation_aliases.values()
        for ref in aliases
    }
    accepted_step_ids = {
        str(observation.get("id"))
        for observation in observations
        if observation.get("status") in {"succeeded", "partial"}
        and observation.get("evidence_ref")
        and observation.get("id")
    }
    hypothesis_candidates: list[dict[str, Any]] = []
    sanitized_candidates: list[dict[str, Any]] = []
    for candidate in payload["root_cause_candidates"]:
        relations, relation_issues = _candidate_relations(candidate, allowed_refs)
        issues.extend(relation_issues)
        candidate_refs = candidate.get("evidence_refs", [])
        if any(ref not in accepted_refs for ref in candidate_refs):
            issues.append("candidate references unknown Evidence")
        related = {item["evidence_ref"] for item in relations}
        related_observations = {
            primary
            for primary, aliases in observation_aliases.items()
            if related & aliases
        }
        relations.extend(
            {"evidence_ref": ref, "relation": "uncertain"}
            for ref in sorted(set(observation_aliases) - related_observations)
        )
        supporting_refs = {
            item["evidence_ref"]
            for item in relations
            if item["relation"] == "supports"
        }
        if any(ref not in accepted_refs for ref in supporting_refs):
            issues.append("candidate support references unavailable Evidence")
        if accepted_refs and not supporting_refs:
            issues.append("candidate has no supporting Evidence")
        hypothesis_candidate = {
            "cause": str(candidate["cause"])[:4000],
            "evidence_relations": relations,
            "unknowns": _texts(candidate.get("unknowns")),
            "next_checks": _texts(candidate.get("next_checks")),
        }
        hypothesis_candidates.append(hypothesis_candidate)
        sanitized = {
            **hypothesis_candidate,
            "confidence": candidate["confidence"],
            "evidence_refs": list(candidate.get("evidence_refs", [])),
        }
        if isinstance(candidate.get("category"), str):
            sanitized["category"] = candidate["category"][:200]
        sanitized_candidates.append(sanitized)

    if not accepted_refs:
        issues.append("final diagnosis has no accepted Evidence")
    if (
        remaining_evidence_steps > 0
        and not missing_evidence
        and any(candidate["next_checks"] for candidate in hypothesis_candidates)
    ):
        issues.append("candidate declares a remaining check while Evidence budget is available")
    actions = payload.get("recommended_actions", [])
    for action in actions:
        step_ids = action.get("evidence_step_ids") if isinstance(action, dict) else None
        if step_ids is None:
            continue
        if (
            not isinstance(step_ids, list)
            or any(not isinstance(step_id, str) for step_id in step_ids)
            or any(step_id not in accepted_step_ids for step_id in step_ids)
        ):
            issues.append("recommended action references unknown Evidence Step")
    try:
        sanitized_actions = normalize_recommendations(actions)
    except ValueError as exc:
        issues.append(str(exc))
        sanitized_actions = []

    for conflicting_refs in _fact_conflicts(observations):
        for candidate in hypothesis_candidates:
            relations = {
                str(item["evidence_ref"]): str(item["relation"])
                for item in candidate["evidence_relations"]
            }
            conflict_relations = {
                next(
                    (
                        relation
                        for ref, relation in relations.items()
                        if ref in observation_aliases.get(primary, {primary})
                    ),
                    "uncertain",
                )
                for primary in conflicting_refs
            }
            if conflict_relations == {"supports"}:
                issues.append("candidate supports contradictory tool facts")

    covered_sources = {
        str(item.get("source_type") or "")
        for item in observations
        if item.get("status") in {"succeeded", "partial"} and _observation_ref(item)
    }
    explained_sources = {
        str(item.get("source_type") or "")
        for item in missing_evidence
        if str(item.get("reason") or "").strip()
    }
    for source in sorted(set(required_sources) - covered_sources - explained_sources):
        issues.append(f"required source was not checked or explained: {source}")

    expected_scope = _incident_scope(incident)
    for observation in observations:
        scope = observation.get("authorized_scope")
        if not isinstance(scope, dict):
            continue
        for field, expected in expected_scope.items():
            actual = scope.get(field)
            if actual not in (None, "", expected):
                issues.append(f"observation scope mismatch: {field}")
        audit = observation.get("audit")
        if isinstance(audit, dict) and audit.get("selector_conflict"):
            issues.append("Kubernetes selector conflicts with the authorized resource scope")

    sanitized_payload = {
        "root_cause_candidates": sanitized_candidates,
        "recommended_actions": sanitized_actions,
        "confidence": payload["confidence"],
    }
    return {
        "payload": sanitized_payload,
        "hypothesis_state": {
            "goal": str(incident.get("summary") or incident.get("alert_name") or "定位 Incident 根因"),
            "candidates": hypothesis_candidates,
        },
        "validation": {
            "status": "accepted" if not issues else "rejected",
            "issues": list(dict.fromkeys(issues)),
            "repair_attempts": 0,
            "stopping_reason": "validated" if not issues else "validation_failed",
            "remaining_evidence_steps": max(0, remaining_evidence_steps),
        },
    }


def safe_completion(
    *,
    incident: dict[str, Any],
    observations: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    issues: list[str],
    required_sources: Iterable[str],
    remaining_evidence_steps: int,
    repair_attempts: int = 1,
    stopping_reason: str = "repair_failed",
) -> dict[str, Any]:
    covered_sources = {
        str(item.get("source_type") or "")
        for item in observations
        if item.get("status") in {"succeeded", "partial"} and item.get("evidence_ref")
    }
    explained_sources = {
        str(item.get("source_type") or "")
        for item in missing_evidence
        if str(item.get("reason") or "").strip()
    }
    for source in sorted(set(required_sources) - covered_sources - explained_sources):
        missing_evidence.append(
            {
                "source_type": source,
                "tool": "model_tooluse",
                "reason": f"model stopped before required {source} source was checked",
                "audit": {"reason_code": "required_source_unchecked"},
            }
        )
    state = initial_hypothesis_state(incident)
    [candidate] = state["candidates"]
    candidate["cause"] = "证据不足，无法形成经过校验的根因结论"
    for observation in observations:
        record_observation(state, observation)
    candidate["unknowns"] = list(dict.fromkeys([
        *issues,
        *(
            f"{item.get('source_type') or 'unknown'} Evidence 不可用"
            for item in missing_evidence
        ),
    ]))
    candidate["next_checks"] = ["由人工核对缺失或冲突的 Evidence 后继续 Investigation"]
    return {
        "payload": {
            "root_cause_candidates": [
                {
                    "cause": candidate["cause"],
                    "confidence": 0.0,
                    "evidence_refs": [],
                }
            ],
            "recommended_actions": [],
            "confidence": {"score": 0.0, "level": "low"},
        },
        "hypothesis_state": state,
        "validation": {
            "status": "safe_partial",
            "issues": list(dict.fromkeys(issues)),
            "repair_attempts": repair_attempts,
            "stopping_reason": stopping_reason,
            "remaining_evidence_steps": max(0, remaining_evidence_steps),
        },
    }


def _candidate_relations(
    candidate: dict[str, Any], allowed_refs: set[str]
) -> tuple[list[dict[str, str]], list[str]]:
    relations: list[dict[str, str]] = []
    issues: list[str] = []
    raw_relations = candidate.get("evidence_relations")
    if raw_relations is None:
        raw_relations = [
            {"evidence_ref": ref, "relation": "supports"}
            for ref in candidate.get("evidence_refs", [])
        ]
    if not isinstance(raw_relations, list):
        return [], ["candidate evidence_relations must be a list"]
    seen: dict[str, str] = {}
    for item in raw_relations:
        if not isinstance(item, dict):
            issues.append("candidate evidence relation must be an object")
            continue
        ref = str(item.get("evidence_ref") or "")
        relation = str(item.get("relation") or "")
        if not ref or relation not in _RELATIONS:
            issues.append("candidate evidence relation is invalid")
            continue
        if ref not in allowed_refs:
            issues.append("candidate references unknown Evidence")
            continue
        if ref in seen and seen[ref] != relation:
            issues.append("candidate assigns conflicting relations to Evidence")
            continue
        if ref not in seen:
            seen[ref] = relation
            relations.append({"evidence_ref": ref, "relation": relation})
    return relations, issues


def _observation_refs(observation: dict[str, Any]) -> set[str]:
    refs = {str(observation.get("id") or "").strip()}
    evidence_ref = observation.get("evidence_ref")
    if isinstance(evidence_ref, dict):
        refs.update(
            str(evidence_ref.get(field) or "").strip()
            for field in ("ref_id", "source_ref", "id")
        )
    elif evidence_ref:
        refs.add(str(evidence_ref).strip())
    refs.discard("")
    return refs


def _observation_ref(observation: dict[str, Any]) -> str:
    refs = _observation_refs(observation)
    evidence_ref = observation.get("evidence_ref")
    if evidence_ref:
        if isinstance(evidence_ref, dict):
            for field in ("ref_id", "source_ref", "id"):
                if evidence_ref.get(field):
                    return str(evidence_ref[field])
        return str(evidence_ref)
    return str(observation.get("id") or "") if refs else ""


def _incident_scope(incident: dict[str, Any]) -> dict[str, str]:
    values = {
        "cluster_id": incident.get("cluster_id") or incident.get("cluster"),
        "namespace": incident.get("namespace"),
        "service": incident.get("service"),
        "workload_kind": incident.get("workload_kind"),
        "workload_name": incident.get("workload_name"),
    }
    return {field: str(values[field]) for field in _SCOPE_FIELDS if values.get(field) not in (None, "")}


def _fact_conflicts(observations: list[dict[str, Any]]) -> list[set[str]]:
    groups: dict[tuple[str, str, str, str], dict[str, set[str]]] = {}
    for observation in observations:
        ref = _observation_ref(observation)
        facts = observation.get("key_facts")
        if not ref or observation.get("status") not in {"succeeded", "partial"} or not isinstance(facts, list):
            continue
        source = str(observation.get("source_type") or "")
        scope = json.dumps(observation.get("authorized_scope") or {}, sort_keys=True, default=str)
        time_range = json.dumps(observation.get("time_range") or {}, sort_keys=True, default=str)
        for fact in facts:
            if not isinstance(fact, dict) or not str(fact.get("name") or ""):
                continue
            key = (source, scope, time_range, str(fact["name"]))
            value = json.dumps(fact.get("value"), sort_keys=True, default=str)
            groups.setdefault(key, {}).setdefault(value, set()).add(ref)
    return [
        set().union(*values.values())
        for values in groups.values()
        if len(values) > 1
    ]


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip()[:1000] for item in value[:20] if isinstance(item, str) and item.strip()]
