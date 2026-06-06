"""Incident diagnosis runtime skeleton for AIO-51."""

from __future__ import annotations

import json
from typing import Any


EVIDENCE_SOURCES = {"metrics", "logs", "topology", "k8s_read"}
MUTATION_KEYWORDS = {
    "apply",
    "delete",
    "exec",
    "patch",
    "restart",
    "rollback",
    "scale",
    "write",
}


def build_diagnosis(
    *,
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]] | None = None,
    memory_hints: list[dict[str, Any]] | None = None,
    recommended_actions: list[dict[str, Any]] | None = None,
    rollback_plan: list[str] | None = None,
    next_verification: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured diagnosis without performing remediation."""
    evidence_chain, missing_sources = _build_evidence_chain(evidence_refs or [])
    hints = [_normalize_hint(item) for item in memory_hints or []]
    candidates = _build_root_cause_candidates(evidence_chain, hints)
    confidence = _score_confidence(evidence_chain, candidates)
    level = _confidence_level(confidence)

    if not evidence_chain:
        candidates = [
            {
                "cause": "insufficient non-memory evidence",
                "confidence": 0.2,
                "evidence_refs": [],
                "optional_hints": [hint["summary"] for hint in hints],
            }
        ]

    diagnosis = {
        "summary": _build_summary(incident, level, evidence_chain),
        "root_cause_candidates": candidates,
        "evidence_chain": evidence_chain,
        "recommended_actions": _normalize_actions(recommended_actions or [], level),
        "rollback_plan": rollback_plan or _default_rollback_plan(),
        "open_questions": _build_open_questions(missing_sources, evidence_chain),
        "next_verification": next_verification or _default_next_verification(missing_sources),
        "confidence": {"score": confidence, "level": level},
        "optional_memory_hints": hints,
        "trace_refs": [],
        "automation": {"unattended_remediation_allowed": False},
    }
    diagnosis["markdown"] = render_markdown(diagnosis)
    return diagnosis


def render_markdown(diagnosis: dict[str, Any]) -> str:
    """Render the diagnosis as readable Markdown while keeping JSON parseable separately."""
    lines = [
        f"# Incident diagnosis: {diagnosis['confidence']['level']}",
        "",
        diagnosis["summary"],
        "",
        "## Root cause candidates",
    ]
    for candidate in diagnosis["root_cause_candidates"]:
        refs = ", ".join(candidate.get("evidence_refs") or ["no direct evidence"])
        lines.append(f"- {candidate['cause']} (confidence={candidate['confidence']:.2f}; evidence={refs})")

    lines.extend(["", "## Evidence chain"])
    if diagnosis["evidence_chain"]:
        for item in diagnosis["evidence_chain"]:
            lines.append(f"- {item['source_type']} `{item['source_ref']}`: {item['summary']}")
    else:
        lines.append("- No non-memory evidence was supplied.")

    lines.extend(["", "## Recommended actions"])
    for action in diagnosis["recommended_actions"]:
        approval = "approval required" if action["approval_required"] else "read-only"
        lines.append(f"- [{approval}] {action['summary']}")

    lines.extend(["", "## Open questions"])
    for question in diagnosis["open_questions"]:
        lines.append(f"- {question}")

    lines.extend(["", "## Next verification"])
    for step in diagnosis["next_verification"]:
        lines.append(f"- {step}")
    return "\n".join(lines)


def to_json(diagnosis: dict[str, Any]) -> str:
    """Serialize diagnosis output for tool callers."""
    return json.dumps(diagnosis, ensure_ascii=False, sort_keys=True)


def _build_evidence_chain(evidence_refs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    chain: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, item in enumerate(evidence_refs, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "").strip()
        if source_type not in EVIDENCE_SOURCES:
            continue
        source_ref = str(item.get("source_ref") or item.get("ref") or f"{source_type}:{index}")
        summary = str(item.get("summary") or item.get("description") or "").strip()
        if not summary:
            summary = f"{source_type} evidence reference {source_ref}"
        seen_sources.add(source_type)
        chain.append(
            {
                "id": f"ev-{index}",
                "source_type": source_type,
                "source_ref": source_ref,
                "summary": summary,
                "payload": item.get("payload") or {},
                "confidence": float(item.get("confidence", 0.6)),
            }
        )
    return chain, sorted(EVIDENCE_SOURCES - seen_sources)


def _normalize_hint(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(item.get("source") or "memory"),
        "summary": str(item.get("summary") or item.get("description") or ""),
        "weight": "optional",
    }


def _build_root_cause_candidates(
    evidence_chain: list[dict[str, Any]],
    hints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text = " ".join(item["summary"].lower() for item in evidence_chain)
    evidence_ids = [item["id"] for item in evidence_chain]
    candidates: list[dict[str, Any]] = []

    if any(token in text for token in ("5xx", "error rate", "timeout", "latency", "payment")):
        candidates.append(
            {
                "cause": "service error-rate regression or upstream dependency failure",
                "confidence": min(0.9, 0.35 + len(evidence_ids) * 0.15),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    if any(token in text for token in ("crashloopbackoff", "oomkilled", "restart", "back-off", "exit code")):
        candidates.append(
            {
                "cause": "workload crash loop caused by application/runtime or resource failure",
                "confidence": min(0.9, 0.35 + len(evidence_ids) * 0.15),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    if not candidates and evidence_chain:
        candidates.append(
            {
                "cause": "undifferentiated incident requiring more evidence",
                "confidence": min(0.55, 0.25 + len(evidence_ids) * 0.1),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    return candidates


def _score_confidence(evidence_chain: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> float:
    if not evidence_chain:
        return 0.2
    source_count = len({item["source_type"] for item in evidence_chain})
    avg_evidence_confidence = sum(item["confidence"] for item in evidence_chain) / len(evidence_chain)
    candidate_bonus = 0.15 if candidates else 0.0
    score = 0.2 + source_count * 0.14 + avg_evidence_confidence * 0.25 + candidate_bonus
    return round(min(score, 0.92), 2)


def _confidence_level(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _build_summary(incident: dict[str, Any], level: str, evidence_chain: list[dict[str, Any]]) -> str:
    alert_name = incident.get("alert_name") or incident.get("name") or "unknown alert"
    namespace = incident.get("namespace") or "unknown namespace"
    cluster = incident.get("cluster") or "unknown cluster"
    return (
        f"{alert_name} in {namespace}/{cluster}: diagnosis confidence is {level} "
        f"based on {len(evidence_chain)} non-memory evidence item(s)."
    )


def _normalize_actions(actions: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    if not actions:
        actions = [{"summary": "Collect missing read-only evidence before remediation.", "action_type": "read"}]

    normalized = []
    for action in actions:
        summary = str(action.get("summary") or action.get("description") or "")
        action_type = str(action.get("action_type") or action.get("type") or "read").lower()
        mutates = bool(action.get("mutates", False)) or action_type in {"mutation", "k8s_write", "write"}
        mutates = mutates or any(keyword in summary.lower() for keyword in MUTATION_KEYWORDS)
        normalized.append(
            {
                "summary": summary,
                "action_type": action_type,
                "approval_required": bool(action.get("approval_required", False)) or mutates,
                "execute_automatically": False,
                "allowed_with_confidence": level != "low" or not mutates,
            }
        )
    return normalized


def _default_rollback_plan() -> list[str]:
    return [
        "Do not execute rollback automatically.",
        "Prepare a human-approved rollback path before any mutation.",
        "Verify service health and alert recovery after approved changes.",
    ]


def _build_open_questions(missing_sources: list[str], evidence_chain: list[dict[str, Any]]) -> list[str]:
    if not evidence_chain:
        return ["Which metrics/logs/topology/k8s_read evidence confirms the current symptom?"]
    return [f"Missing {source} evidence for cross-checking." for source in missing_sources]


def _default_next_verification(missing_sources: list[str]) -> list[str]:
    if missing_sources:
        return [f"Collect {source} evidence ref." for source in missing_sources[:2]]
    return ["Re-check symptoms after any approved remediation.", "Confirm alert recovery from read-only signals."]
