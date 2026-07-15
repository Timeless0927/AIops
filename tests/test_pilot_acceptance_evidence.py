from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from aiops.acceptance.evidence import (
    A01_GATE_SEQUENCE,
    GATE_PHASE,
    AcceptanceEvidence,
    EvidenceError,
)
from aiops.acceptance.redaction import redact_json, redact_text


def _ledger(tmp_path: Path) -> AcceptanceEvidence:
    return AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-20260714T010203Z",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
    )


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


def test_gate_attempts_are_append_only_and_failed_run_cannot_promote(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    first = evidence.record_gate("P01", "failed", [evidence.write_text("P01", "check.txt", "bad")])
    second = evidence.record_gate("P01", "passed", [evidence.write_text("P01", "retry.txt", "ok")])

    manifest = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    assert first.attempt == 1
    assert second.attempt == 2
    assert [attempt["status"] for attempt in manifest["gates"]["P01"]] == ["failed", "passed"]
    assert manifest["promotion_eligible"] is False

    reopened = AcceptanceEvidence.open(evidence.root, now=lambda: "2026-07-14T01:03:03Z")
    with pytest.raises(EvidenceError, match="not frontier"):
        reopened.record_gate("P02", "passed", [])
    with pytest.raises(EvidenceError, match="candidate identity"):
        AcceptanceEvidence.create(
            evidence.root.parent,
            acceptance_id=evidence.root.name,
            release_version="v0.1.1",
            release_sha256="c" * 64,
            kube_context="pilot-clean",
            cluster_identity_sha256="b" * 64,
            access_profile="http_nodeport",
        )


def test_artifacts_are_hashed_and_only_i04_may_be_not_applicable(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    _advance(evidence, "I04")
    artifact = evidence.write_json("I04", "https-profile.json", {"declared": False})
    evidence.record_gate("I04", "not_applicable", [artifact])

    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert artifact.relative_path == "01-install/I04-attempt-1/https-profile.json"
    with pytest.raises(EvidenceError, match="only I04"):
        evidence.record_gate("P01", "not_applicable", [])
    with pytest.raises(EvidenceError, match="artifact name"):
        evidence.write_text("P01", "../escape.txt", "no")


def test_text_and_json_redaction_remove_headers_cookies_recipients_and_known_secrets(
    tmp_path: Path,
) -> None:
    secret = "sk-live-secret-value"
    raw = (
        "Authorization: Bearer token-value\n"
        "Cookie: aiops_session=session-value\n"
        "Set-Cookie: aiops_session=other-value; HttpOnly\n"
        f"api_key={secret}\n"
    )
    redacted = redact_text(raw, known_secrets=[secret])
    assert "token-value" not in redacted
    assert "session-value" not in redacted
    assert "other-value" not in redacted
    assert secret not in redacted

    payload = redact_json(
        {
            "request_id": "request-1",
            "api_key": secret,
            "recipient": "person@example.test",
            "nested": {"password": "password", "reason_code": "authentication_failed"},
            "wrong_password": 401,
            "stale_auth_matrix": {"model": 403, "notification": 403},
            "credential_configured": True,
            "model_response": "raw provider output",
        },
        known_secrets=[secret],
    )
    assert payload == {
        "request_id": "request-1",
        "api_key": "[REDACTED]",
        "recipient": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "reason_code": "authentication_failed"},
        "wrong_password": 401,
        "stale_auth_matrix": {"model": 403, "notification": 403},
        "credential_configured": True,
        "model_response": "[REDACTED]",
    }

    evidence = _ledger(tmp_path)
    artifact = evidence.write_text("P01", "safe.txt", raw, known_secrets=[secret])
    persisted = artifact.path.read_text(encoding="utf-8")
    assert secret not in persisted and "session-value" not in persisted


def test_signed_attestations_and_final_checksums_are_deterministic(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    _advance(evidence, "I05")
    artifact = evidence.write_text("I05", "auth-matrix.txt", "anonymous=401\n")
    evidence.record_gate("I05", "passed", [artifact])

    statement = evidence.attestation_statement(
        actor="operator@example.test",
        role="platform_administrator",
        gate_ids=["I05"],
        conclusion="passed",
        note="clean capacity/CNI and first login observed",
    )
    evidence.append_attestation(
        statement,
        signature="test-signature",
        public_key="ssh-ed25519 AAAATEST operator@example.test",
        fingerprint="SHA256:test-fingerprint",
    )
    for gate_id, role in (
        ("P03", "platform_operator"),
        ("S04", "platform_administrator"),
        ("S05", "platform_operator"),
    ):
        extra = evidence.attestation_statement(
            actor=f"{gate_id.lower()}@example.test",
            role=role,
            gate_ids=[gate_id],
            conclusion="passed",
            note=f"{gate_id} observed",
        )
        evidence.append_attestation(
            extra,
            signature=f"signature-{gate_id}",
            public_key=f"ssh-ed25519 AAAATEST{gate_id}",
            fingerprint=f"SHA256:{gate_id}",
        )
    for gate_id in A01_GATE_SEQUENCE[A01_GATE_SEQUENCE.index("S01") :]:
        evidence.record_gate(gate_id, "passed", [])
    verified: list[str] = []
    checksum_path = evidence.finalize(
        lambda item: verified.append(item["statement"]["actor"])
    )
    assert evidence.promotion_eligible is False

    attestations = yaml.safe_load(evidence.attestation_path.read_text(encoding="utf-8"))
    assert attestations["attestations"][0]["statement"] == statement
    assert attestations["attestations"][0]["signature"] == "test-signature"
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    indexed = {line.split("  ", 1)[1] for line in lines}
    assert "manifest.json" in indexed
    assert "human-attestation.yaml" in indexed
    assert "SHA256SUMS" not in indexed
    for line in lines:
        digest, relative = line.split("  ", 1)
        assert digest == hashlib.sha256((evidence.root / relative).read_bytes()).hexdigest()
    assert len(verified) == 4


def test_promotion_requires_recovery_two_runs_and_cleanup(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    for gate_id in A01_GATE_SEQUENCE:
        evidence.record_gate(
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
            [],
        )
    assert evidence.promotion_eligible is False

    for gate_id in GATE_PHASE:
        if gate_id not in A01_GATE_SEQUENCE:
            evidence.record_gate(gate_id, "passed", [])
    assert evidence.promotion_eligible is True


def test_open_rejects_tampered_eligibility_and_artifact_hash(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    artifact = evidence.write_text("P01", "check.txt", "safe")
    evidence.record_gate("P01", "passed", [artifact])
    manifest = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    manifest["promotion_eligible"] = True
    evidence.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(EvidenceError, match="promotion eligibility"):
        AcceptanceEvidence.open(evidence.root)

    manifest["promotion_eligible"] = False
    evidence.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifact.path.write_text("tampered", encoding="utf-8")
    with pytest.raises(EvidenceError, match="hash or size"):
        AcceptanceEvidence.open(evidence.root)


def test_https_profile_cannot_mark_i04_not_applicable(tmp_path: Path) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="https-conditional",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile="https_ingress",
    )
    _advance(evidence, "I04")
    with pytest.raises(EvidenceError, match="only for http_nodeport"):
        evidence.record_gate("I04", "not_applicable", [])


def test_attestation_is_verified_before_gate_consumes_claim(tmp_path: Path) -> None:
    def reject(_item):
        raise RuntimeError("bad signature")

    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="verified-before-use",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        attestation_verifier=reject,
    )
    statement = evidence.attestation_statement(
        actor="operator@example.test",
        role="platform_operator",
        gate_ids=["P03"],
        conclusion="passed",
        note="manual boundary observed",
    )
    evidence.append_attestation(
        statement,
        signature="tampered",
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:test",
    )
    with pytest.raises(RuntimeError, match="bad signature"):
        evidence.require_verified_attestation("P03", role="platform_operator")
