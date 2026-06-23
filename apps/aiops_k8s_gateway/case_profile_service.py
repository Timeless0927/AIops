"""Gateway-side confirmed root-cause backfill (评测集标签 C).

运维/研发在故障解决后回填确认的真根因,作为评测集的正确答案标签。落
`incident_store.upsert_case_profile`;`root_cause_category` 是回放按类目带容差
打分的关键字段。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from toolsets import incident_store


JSON = dict[str, Any]

# 回填端点 incident_id 放 body,刻意绕开 ADR-0004 的路径参数路由改造前置。
REQUIRED_FIELDS = ("incident_id", "final_root_cause", "root_cause_category")


def validate_case_profile_payload(payload: JSON) -> tuple[HTTPStatus, JSON] | None:
    """校验真根因回填 body;必填字段缺失或类型不符即拒绝。"""
    incident_id = str(payload.get("incident_id") or "").strip()
    if not incident_id:
        return HTTPStatus.BAD_REQUEST, {
            "ok": False,
            "status": "invalid",
            "error": "incident_id is required",
        }

    final_root_cause = str(payload.get("final_root_cause") or "").strip()
    if not final_root_cause:
        return HTTPStatus.BAD_REQUEST, {
            "ok": False,
            "status": "invalid",
            "error": "final_root_cause is required",
        }

    root_cause_category = str(payload.get("root_cause_category") or "").strip()
    if not root_cause_category:
        return HTTPStatus.BAD_REQUEST, {
            "ok": False,
            "status": "invalid",
            "error": "root_cause_category is required",
        }

    key_evidence_refs = payload.get("key_evidence_refs")
    if key_evidence_refs is None:
        key_evidence_refs = []
    if not isinstance(key_evidence_refs, list) or not all(
        isinstance(ref, str) for ref in key_evidence_refs
    ):
        return HTTPStatus.BAD_REQUEST, {
            "ok": False,
            "status": "invalid",
            "error": "key_evidence_refs must be a list of strings",
        }

    effective_actions = payload.get("effective_actions")
    if effective_actions is None:
        effective_actions = []
    if not isinstance(effective_actions, list) or not all(
        isinstance(action, str) for action in effective_actions
    ):
        return HTTPStatus.BAD_REQUEST, {
            "ok": False,
            "status": "invalid",
            "error": "effective_actions must be a list of strings",
        }
    return None


def _derive_incident_signature(incident: dict[str, Any]) -> str:
    """incident_signature 存储要求 NOT NULL;body 不带则按 incident 字段拼。"""
    return "|".join(
        part
        for part in (
            str(incident.get("alert_name") or ""),
            str(incident.get("namespace") or ""),
            str(incident.get("cluster") or ""),
            "resolved",
        )
        if part
    )


async def apply_case_profile(payload: JSON, *, store: Any = incident_store) -> tuple[HTTPStatus, JSON]:
    """落库真根因回填:upsert_case_profile。"""
    invalid = validate_case_profile_payload(payload)
    if invalid is not None:
        return invalid

    incident_id = str(payload["incident_id"]).strip()
    try:
        incident = await store.get_incident(incident_id)
    except ValueError as exc:
        return HTTPStatus.NOT_FOUND, {"ok": False, "status": "not_found", "error": str(exc)}
    if incident is None:
        return HTTPStatus.NOT_FOUND, {
            "ok": False,
            "status": "not_found",
            "error": f"事件不存在: {incident_id}",
        }

    signature = str(payload.get("incident_signature") or "").strip() or _derive_incident_signature(
        dict(incident)
    )
    try:
        await store.upsert_case_profile(
            incident_id,
            incident_signature=signature,
            final_root_cause=str(payload["final_root_cause"]).strip(),
            root_cause_category=str(payload["root_cause_category"]).strip(),
            key_evidence_refs=list(payload.get("key_evidence_refs") or []),
            effective_actions=list(payload.get("effective_actions") or []),
        )
    except ValueError as exc:
        return HTTPStatus.NOT_FOUND, {"ok": False, "status": "not_found", "error": str(exc)}

    return HTTPStatus.OK, {
        "ok": True,
        "status": "persisted",
        "incident_id": incident_id,
        "incident_signature": signature,
    }


async def read_case_profile(incident_id: str, *, store: Any = incident_store) -> tuple[HTTPStatus, JSON]:
    """读回 case profile 供验收核对。"""
    profile = await store.get_case_profile(incident_id)
    if profile is None:
        return HTTPStatus.NOT_FOUND, {
            "ok": False,
            "status": "not_found",
            "error": f"case profile 不存在: {incident_id}",
        }
    return HTTPStatus.OK, {"ok": True, "status": "ok", "case_profile": profile}