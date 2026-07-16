"""Durable gate-operation journal inside the Acceptance Evidence Module."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .redaction import redact_json


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPERATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class JournalError(ValueError):
    pass


def valid_operation_id(value: str) -> bool:
    return _OPERATION_PATTERN.fullmatch(value) is not None


def new_attempt(execution_id: str, started_at: str) -> dict[str, Any]:
    return {
        "attempt": 1,
        "execution_id": execution_id,
        "status": "open",
        "started_at": started_at,
        "operations": [{
            "kind": "gate_execution",
            "operation_id": execution_id,
            "bound_at": started_at,
        }],
        "reconciliations": [],
        "artifacts": [],
    }


def bind(
    manifest: dict[str, Any],
    attempt: dict[str, Any],
    *,
    kind: str,
    operation_id: str,
    now: Callable[[], str],
) -> None:
    if not _ID_PATTERN.fullmatch(kind) or not valid_operation_id(operation_id):
        raise JournalError("operation identity is invalid")
    if any(
        item["operation_id"] == operation_id
        for attempts in manifest["gates"].values()
        for item in attempts[0].get("operations", [])
    ):
        raise JournalError("operation identity is already bound; duplicate effect rejected")
    attempt["operations"].append({
        "kind": kind,
        "operation_id": operation_id,
        "bound_at": now(),
    })


def reconcile(
    attempt: dict[str, Any],
    *,
    operation_id: str,
    outcome: str,
    public_fact: dict[str, Any],
    now: Callable[[], str],
) -> None:
    if operation_id not in {item["operation_id"] for item in attempt["operations"]}:
        raise JournalError("reconciliation does not match a durable operation identity")
    if outcome not in {"succeeded", "failed", "unprovable"}:
        raise JournalError("reconciliation outcome is invalid")
    if any(item["operation_id"] == operation_id for item in attempt["reconciliations"]):
        raise JournalError("operation already has a reconciliation fact")
    fact = redact_json(public_fact)
    if not isinstance(fact, dict) or not fact or len(json.dumps(fact).encode()) > 64 * 1024:
        raise JournalError("reconciliation public fact must be a bounded object")
    attempt["reconciliations"].append({
        "operation_id": operation_id,
        "outcome": outcome,
        "public_fact": fact,
        "recorded_at": now(),
    })


def validation_error(attempt: dict[str, Any]) -> str | None:
    operations = attempt.get("operations")
    if not isinstance(operations, list) or not operations:
        return "gate operation journal is invalid"
    operation_ids = [
        str(item.get("operation_id", "")) for item in operations if isinstance(item, dict)
    ]
    execution_id = attempt.get("execution_id")
    if (
        len(operation_ids) != len(operations)
        or len(set(operation_ids)) != len(operation_ids)
        or sum(
            item.get("kind") == "gate_execution" and item.get("operation_id") == execution_id
            for item in operations
        ) != 1
    ):
        return "gate execution operation identity is missing or duplicated"
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or not valid_operation_id(str(operation.get("operation_id", "")))
            or not _ID_PATTERN.fullmatch(str(operation.get("kind", "")))
            or not isinstance(operation.get("bound_at"), str)
        ):
            return "gate operation identity is invalid"
    reconciliations = attempt.get("reconciliations")
    if not isinstance(reconciliations, list):
        return "gate reconciliation journal is invalid"
    known_operation_ids = set(operation_ids)
    reconciliation_ids: set[str] = set()
    for item in reconciliations:
        operation_id = str(item.get("operation_id", "")) if isinstance(item, dict) else ""
        if (
            not isinstance(item, dict)
            or operation_id not in known_operation_ids
            or operation_id in reconciliation_ids
            or item.get("outcome") not in {"succeeded", "failed", "unprovable"}
            or not isinstance(item.get("public_fact"), dict)
            or not item["public_fact"]
            or len(json.dumps(item["public_fact"]).encode()) > 64 * 1024
            or not isinstance(item.get("recorded_at"), str)
        ):
            return "gate reconciliation fact is invalid"
        reconciliation_ids.add(operation_id)
    return None
