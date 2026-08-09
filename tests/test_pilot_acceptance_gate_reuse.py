from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.evidence_types import GateResult
from aiops.acceptance.deployment_continuation import validate_bundle
from aiops.acceptance.ledger import AcceptanceLedger
from aiops.acceptance.evidence_files import sha256, sha256_bytes
from aiops.acceptance.gate_contract import (
    GATE_CONTRACT_REVISION,
    GATE_REUSE_POLICIES,
    GATE_SEQUENCE,
)
from aiops.acceptance.gate_reuse import freeze_reuse_plan, reuse_gate
from aiops.acceptance.promotion import PromotionDecision
from tests.pilot_acceptance_support import create_evidence, qualified_continuation


NOW = datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc)


def _verify(item: dict) -> None:
    if item.get("signature") != "valid-signature":
        raise ValueError("invalid signature")


def _source(
    tmp_path: Path,
    *,
    name: str = "source-no-promote",
    failed_gate: str = "I05",
    gate_operations: dict[str, list[str]] | None = None,
    failure_attribution: str = "tool_failure",
) -> AcceptanceLedger:
    ids = count(1)
    operations = gate_operations or {failed_gate: ["request-user-1"]}
    source = create_evidence(
        tmp_path / "source",
        acceptance_id=name,
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="3" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-19T13:00:00Z",
        new_execution_id=lambda: f"source-execution-{next(ids)}",
        attestation_verifier=_verify,
    )
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index(failed_gate)]:
        started_at = source.start_gate(gate_id)
        for operation_id in operations.get(gate_id, []):
            source.bind_operation(
                gate_id, kind="test_mutation", operation_id=operation_id,
            )
        artifacts = []
        if gate_id in GATE_REUSE_POLICIES:
            artifact_name = "package.json" if gate_id == "P01" else "evidence.json"
            artifacts.append(
                source.write_json(gate_id, artifact_name, {"gate_id": gate_id})
            )
        source.record_gate(gate_id, GateResult("not_applicable" if gate_id == "I04" else "passed", tuple(artifacts)), started_at=started_at)
    source.start_gate(failed_gate)
    for operation_id in operations.get(failed_gate, []):
        source.bind_operation(
            failed_gate, kind="test_mutation", operation_id=operation_id,
        )
    source.record_gate(failed_gate, GateResult("failed", (), failure_attribution))
    source.evaluate()
    decision = PromotionDecision(source)
    statement = decision.statement(
        actor="release-owner@example.test", decision="no_promote", note="tool failed",
    )
    decision.record(
        statement,
        signature="valid-signature",
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:test",
    )
    source.seal()
    return source


def _continuation(
    source: AcceptanceLedger,
    plan: list[dict[str, object]],
    *,
    recovered_operations: tuple[str, ...] = (),
) -> dict:
    bundle = qualified_continuation(
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    record = bundle["record"]
    failure = source.failure_summary()
    operation_ids = [*failure["issued_operation_ids"], *recovered_operations]
    record["source"].update({
        "acceptance_id": source.root.name,
        "failed_gate": failure["gate_id"],
        "seal_sha256": sha256(source.root / "SHA256SUMS"),
        "product_sha256": source.candidate_sha256,
        "acceptance_tool_sha256": source.acceptance_tool_sha256,
        "failure_attribution": failure["failure_attribution"],
        "issued_operation_ids": operation_ids,
    })
    record["reconciliations"] = [
        {
            "operation_id": operation_id,
            "request_id": operation_id,
            "object_identity": {"object.id": f"object-{index}"},
            "revision_identity": {"object.updated_at": "2026-07-01T00:00:00Z"},
            "outcome": "succeeded",
            "unknown_side_effects": False,
            "irreversible_side_effects": False,
        }
        for index, operation_id in enumerate(operation_ids, start=1)
    ]
    record["reusable_gates"] = freeze_reuse_plan(
        source, plan, failed_gate=failure["gate_id"], reconciled=set(operation_ids),
    )
    return _resign_continuation(bundle)


def test_one_continuation_signature_reuses_frontier_without_replaying_effect(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    target = _target(tmp_path, continuation)

    result = reuse_gate(
        source=source,
        target=target,
        continuation=continuation,
        now=lambda: NOW,
        verifier=_verify,
    )

    assert result == {"gate_id": "P01", "status": "passed", "reused": True}
    assert target.frontier == "P02"
    assert target.passed_artifact_json("P01", "package.json")["value"] == {
        "gate_id": "P01",
    }
    provenance = target.passed_artifact_json(
        "P01", f"reuse-{source.root.name}.json",
    )["value"]
    assert provenance["deployment_continuation_bundle_sha256"] == continuation[
        "bundle_sha256"
    ]
    assert provenance["effect_replayed"] is False


def test_continuation_rejects_gate_without_opt_in_policy(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(ValueError, match="not reusable"):
        _continuation(source, [{"gate_id": "I05", "operation_ids": []}])


def test_reuse_requires_signed_continuation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    continuation["attestation"] = None
    continuation["bundle_sha256"] = sha256_bytes(_json_bytes({
        key: continuation[key] for key in ("record", "record_sha256", "attestation")
    }))

    with pytest.raises(ValueError, match="attestation"):
        reuse_gate(
            source=source,
            target=_target(tmp_path, _continuation(
                source, [{"gate_id": "P01", "operation_ids": []}],
            )),
            continuation=continuation,
            now=lambda: NOW,
            verifier=_verify,
        )


def test_plan_rejects_source_operation_omitted_from_stable_gate(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        gate_operations={"P01": ["package/publish:1"], "I05": ["request-user-1"]},
    )
    with pytest.raises(ValueError, match="operation inventory"):
        _continuation(source, [{"gate_id": "P01", "operation_ids": []}])


def test_plan_requires_reconciled_effect_for_s01(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        failed_gate="S03",
        gate_operations={"S01": ["notification/skip:1"], "S03": ["request-user-1"]},
    )
    with pytest.raises(ValueError, match="operation accounting"):
        _continuation(source, [{"gate_id": "S01", "operation_ids": []}])


def test_reuse_accepts_s01_effect_recovered_by_signed_continuation(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path, failed_gate="S03", failure_attribution="inconclusive",
    )
    operation_id = "acceptance-s01-notification-skip"
    continuation = _continuation(
        source,
        [{"gate_id": "S01", "operation_ids": [operation_id]}],
        recovered_operations=(operation_id,),
    )
    target = _target(tmp_path, continuation)
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("S01")]:
        started_at = target.start_gate(gate_id)
        target.record_gate(gate_id, GateResult("not_applicable" if gate_id == "I04" else "passed", ()), started_at=started_at)

    result = reuse_gate(
        source=source,
        target=target,
        continuation=continuation,
        now=lambda: NOW,
        verifier=_verify,
    )

    assert result == {"gate_id": "S01", "status": "passed", "reused": True}
    provenance = target.passed_artifact_json(
        "S01", f"reuse-{source.root.name}.json",
    )["value"]
    assert provenance["gate"]["operation_ids"] == [operation_id]


def test_reuse_rejects_source_artifact_tamper(tmp_path: Path) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    artifact = source.root / source.terminal_gate_fact("P01")["artifacts"][0]["path"]
    artifact.chmod(0o644)
    artifact.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact"):
        reuse_gate(
            source=source,
            target=_target(tmp_path, continuation),
            continuation=continuation,
            now=lambda: NOW,
            verifier=_verify,
        )


def test_reuse_rejects_different_target_continuation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    other = _continuation(source, [])
    other["record"]["source"]["diagnostic_sha256"] = "d" * 64
    other = _resign_continuation(other)

    with pytest.raises(ValueError, match="replacement identity drifted"):
        reuse_gate(
            source=source,
            target=_target(tmp_path, other),
            continuation=continuation,
            now=lambda: NOW,
            verifier=_verify,
        )


def test_reuse_rejects_different_sealed_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    other = _source(tmp_path, name="different-source")

    with pytest.raises(ValueError, match="source identity drifted"):
        reuse_gate(
            source=other,
            target=_target(tmp_path, continuation),
            continuation=continuation,
            now=lambda: NOW,
            verifier=_verify,
        )


def test_reuse_rejects_expired_or_wrongly_signed_continuation(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    with pytest.raises(ValueError, match="expired"):
        validate_bundle(
            continuation,
            now=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
            verifier=_verify,
        )
    with pytest.raises(ValueError, match="signature is invalid"):
        validate_bundle(continuation, now=lambda: NOW, verifier=lambda _item: 1 / 0)


def test_reuse_resumes_local_artifact_copy_without_external_replay(
    tmp_path: Path, monkeypatch,
) -> None:
    source = _source(tmp_path)
    continuation = _continuation(
        source, [{"gate_id": "P01", "operation_ids": []}],
    )
    target = _target(tmp_path, continuation)
    monkeypatch.setattr(
        target,
        "write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        reuse_gate(
            source=source,
            target=target,
            continuation=continuation,
            now=lambda: NOW,
            verifier=_verify,
        )
    assert target.open_gate == "P01"

    reopened = AcceptanceLedger.open(
        target.root,
        now=lambda: "2026-07-19T14:06:00Z",
        attestation_verifier=_verify,
    )
    assert reuse_gate(
        source=source,
        target=reopened,
        continuation=continuation,
        now=lambda: NOW,
        verifier=_verify,
    ) == {"gate_id": "P01", "status": "passed", "reused": True}


def _target(tmp_path: Path, continuation: dict) -> AcceptanceLedger:
    return create_evidence(
        tmp_path / "target",
        acceptance_id="replacement-run",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        deployment_continuation=continuation,
        now=lambda: "2026-07-19T14:05:00Z",
        attestation_verifier=_verify,
    )


def _resign_continuation(bundle: dict) -> dict:
    record = bundle["record"]
    record_sha = sha256_bytes(_json_bytes(record))
    statement = bundle["attestation"]["statement"]
    statement.update({
        "record_sha256": record_sha,
        "source": record["source"],
        "replacement": record["replacement"],
        "cluster": record["cluster"],
        "access_profile": record["access_profile"],
        "deployment_identity": record["deployment_identity"],
        "reusable_gates": record["reusable_gates"],
        "conclusion": "retain_existing_and_reuse_exact_gates",
    })
    unsigned = {
        "record": record,
        "record_sha256": record_sha,
        "attestation": bundle["attestation"],
    }
    return {**unsigned, "bundle_sha256": sha256_bytes(_json_bytes(unsigned))}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
