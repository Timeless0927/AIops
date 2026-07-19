"""Durable effect and attestation journal shared by Recovery gates."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from .evidence import AcceptanceEvidence
from .evidence_types import Artifact, GateExecution


class RecoveryJournal:
    """Binds one Recovery effect before dispatch and reconciles it without replay."""

    def __init__(self, evidence: AcceptanceEvidence) -> None:
        self.evidence = evidence

    def effect(
        self,
        gate_id: str,
        execution: GateExecution,
        artifacts: list[Artifact],
        *,
        operation_id: str,
        kind: str,
        artifact_name: str,
        dispatch: Callable[[], dict[str, object]],
        reconcile: Callable[[], dict[str, object] | None],
    ) -> dict[str, object]:
        existing = self.optional_artifact_json(artifacts, artifact_name)
        reconciliation = next(
            (item for item in execution.reconciliations if item["operation_id"] == operation_id),
            None,
        )
        bound = any(item["operation_id"] == operation_id for item in execution.operations)
        if reconciliation is not None:
            if reconciliation.get("outcome") != "succeeded":
                raise ValueError(f"{gate_id} operation {operation_id} is not provably successful")
            fact = reconciliation.get("public_fact")
            result = fact.get("result") if isinstance(fact, dict) else None
        elif bound:
            result = reconcile()
            if result is None:
                self.evidence.reconcile_operation(
                    gate_id, operation_id=operation_id, outcome="unprovable",
                    public_fact={"operation_id": operation_id, "terminal": False},
                )
                raise ValueError(f"{gate_id} interrupted operation is unprovable")
            self.evidence.reconcile_operation(
                gate_id, operation_id=operation_id, outcome="succeeded",
                public_fact={"operation_id": operation_id, "result": result},
            )
        else:
            self.evidence.bind_operation(gate_id, kind=kind, operation_id=operation_id)
            result = dispatch()
            self.evidence.reconcile_operation(
                gate_id, operation_id=operation_id, outcome="succeeded",
                public_fact={"operation_id": operation_id, "result": result},
            )
        if not isinstance(result, dict):
            raise ValueError(f"{gate_id} operation result is invalid")
        if existing is not None and existing != result:
            raise ValueError(f"{gate_id} retained operation result drifted")
        if existing is None:
            artifacts.append(self.evidence.write_json(gate_id, artifact_name, result))
        return result

    def require_operator_attestation(self, gate_id: str, review_sha256: str) -> None:
        self.require_bound_attestation(
            gate_id,
            role="platform_operator",
            note=f"recovery_review_sha256={review_sha256}",
        )

    def require_bound_attestation(
        self, gate_id: str, *, role: str, note: str,
    ) -> None:
        attestations = self.evidence.require_verified_attestation(
            gate_id, role=role,
        )
        if not any(item.get("statement", {}).get("note") == note for item in attestations):
            raise ValueError(f"{gate_id} {role} attestation did not bind the review")

    @staticmethod
    def operation_id(gate_id: str, execution_id: str, owner: str) -> str:
        execution_hash = hashlib.sha256(execution_id.encode()).hexdigest()[:24]
        return f"{gate_id.lower()}/{execution_hash}/{owner}"

    @staticmethod
    def artifact(artifacts: list[Artifact], name: str) -> Artifact:
        matches = [item for item in artifacts if item.path.name == name]
        if len(matches) != 1:
            raise ValueError(f"Recovery durable {name} artifact is missing or duplicated")
        return matches[0]

    @classmethod
    def artifact_json(cls, artifacts: list[Artifact], name: str) -> dict[str, object]:
        value = json.loads(cls.artifact(artifacts, name).path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"Recovery durable {name} artifact is invalid")
        return value

    @classmethod
    def optional_artifact_json(
        cls, artifacts: list[Artifact], name: str,
    ) -> dict[str, object] | None:
        matches = [item for item in artifacts if item.path.name == name]
        if not matches:
            return None
        return cls.artifact_json(artifacts, name)
