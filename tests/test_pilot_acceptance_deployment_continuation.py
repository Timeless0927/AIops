from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.deployment_continuation import (
    DeploymentContinuation,
    conclude_diagnostic_bundle,
    create_diagnostic_bundle,
    write_record,
)
from aiops.acceptance.deployment_observation import observe_existing_deployment
from aiops.acceptance.evidence import AcceptanceEvidence, GATE_CONTRACT_REVISION
from aiops.acceptance.gate_contract import EVIDENCE_FORMAT_VERSION
from aiops.acceptance.promotion import PromotionDecision
from tests.pilot_acceptance_support import create_evidence


NOW = datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)
CLUSTER_IDENTITY = "b" * 64
UNUSED_RELEASE = Path("unused-release")
REAL_OBSERVE_DEPLOYMENT = observe_existing_deployment


def _verify(item: dict) -> None:
    if item.get("signature") != "valid-signature":
        raise ValueError("invalid signature")


def _deployment_identity(
    source, rendered_manifest_sha256: str, deployment_images_sha256: str,
    **changes: object,
) -> dict[str, object]:
    value = {
        "product_sha256": source.candidate_sha256,
        "cluster_identity_sha256": source.cluster_identity_sha256,
        "rendered_manifest_sha256": rendered_manifest_sha256,
        "deployment_images_sha256": deployment_images_sha256,
        "deployment_configuration_sha256": "e" * 64,
        "health_snapshot_sha256": "f" * 64,
        "manifest_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "manifest_diff_exit_code": 0,
        "manifest_diff_server_generation_only": False,
        "healthy": True,
        "observed_at": "2026-07-19T03:00:00Z",
    }
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def _read_only_deployment_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def observe(
        source, _commands, _archive, _checksums, *,
        rendered_manifest_sha256, deployment_images_sha256, observed_at,
    ):
        return _deployment_identity(
            source, rendered_manifest_sha256, deployment_images_sha256,
        )

    monkeypatch.setattr(
        "aiops.acceptance.deployment_observation.observe_existing_deployment", observe,
    )


def _source(
    tmp_path: Path, *, failure_attribution: str | None = None,
    diagnosed_attribution: str = "tool_failure",
    proof_payload: str = '{"response_lost_after_successful_create":true}\n',
    bind_operation: bool = True,
    recovered_operation_ids: tuple[str, ...] = (),
    operation_accounting_complete: bool = True,
    prior_operation_id: str | None = None,
) -> tuple[AcceptanceEvidence, Path]:
    ids = count(1)
    evidence = create_evidence(
        tmp_path / "acceptance",
        acceptance_id="source-tool-failure",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256=CLUSTER_IDENTITY,
        access_profile="http_nodeport",
        now=lambda: "2026-07-19T03:00:00Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=_verify,
    )
    for gate_id in ("P01", "P02", "I01", "I02", "I03", "I04"):
        evidence.start_gate(gate_id)
        manifest = b"kind: List\n"
        artifacts = [
            evidence.write_json("P01", "artifact-inventory.json", [{
                "path": "manifest.yaml", "sha256": hashlib.sha256(manifest).hexdigest(),
                "bytes": len(manifest),
            }]),
            evidence.write_json("P01", "image-list.json", [
                "registry.example.test/aiops/gateway@sha256:" + "1" * 64,
            ]),
        ] if gate_id == "P01" else []
        if gate_id == "I03" and prior_operation_id is not None:
            evidence.bind_operation(
                "I03", kind="setup_mutation", operation_id=prior_operation_id,
            )
        evidence.record_gate(
            gate_id, "not_applicable" if gate_id == "I04" else "passed", artifacts,
        )
    evidence.start_gate("I05")
    if bind_operation:
        evidence.bind_operation(
            "I05", kind="browser_mutation", operation_id="request-user-1",
        )
    evidence.record_gate(
        "I05", "failed", [], failure_attribution=failure_attribution,
    )
    diagnostic = create_diagnostic_bundle(
        evidence, tmp_path / "diagnostics", diagnostic_id="diag-i05",
    )
    proof = diagnostic / "runner-proof.json"
    proof.write_text(proof_payload)
    conclude_diagnostic_bundle(
        diagnostic,
        diagnosed_attribution=diagnosed_attribution,
        conclusion_note="runner lost a successful create-user response",
        evidence=[proof],
        recovered_operation_ids=recovered_operation_ids,
        operation_accounting_complete=operation_accounting_complete,
    )
    evidence.evaluate()
    decision = PromotionDecision(evidence)
    statement = decision.statement(
        actor="release-owner@example.test", decision="no_promote",
        note="tool failure requires replacement evidence",
    )
    decision.record(
        statement, signature="valid-signature",
        public_key="ssh-ed25519 AAAATEST", fingerprint="SHA256:test",
    )
    evidence.seal()
    return evidence, diagnostic


def _reconciliation(operation_id: str = "request-user-1", **changes: object) -> dict:
    value = {
        "operation_id": operation_id,
        "request_id": operation_id,
        "object_identity": {"user.id": "user-1"},
        "revision_identity": {"user.updated_at": "2026-07-19T02:59:00Z"},
        "outcome": "succeeded",
        "unknown_side_effects": False,
        "irreversible_side_effects": False,
    }
    value.update(changes)
    return value


def _replacement(product: str = "a" * 64) -> dict[str, object]:
    return {
        "product_sha256": product,
        "acceptance_tool_sha256": "d" * 64,
        "gate_contract_revision": GATE_CONTRACT_REVISION,
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
    }


def _owner(root: Path) -> DeploymentContinuation:
    return DeploymentContinuation(
        root, commands=object(), now=lambda: NOW,  # type: ignore[arg-type]
    )


def test_tool_failure_creates_signed_epoch_without_inheriting_old_gates(
    tmp_path: Path,
) -> None:
    source, diagnostic = _source(tmp_path)
    owner = _owner(tmp_path / "epochs")
    path = owner.create(
        epoch_id="epoch-i05", source=source, diagnostic=diagnostic,
        replacement=_replacement(), reconciliations=[_reconciliation()],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    statement = owner.attestation_statement(
        path, actor="operator@example.test", note="reviewed exact deployment handoff",
    )
    owner.attach_attestation(path, {
        "statement": statement,
        "signature": "valid-signature",
        "public_key": "ssh-ed25519 AAAATEST",
        "fingerprint": "SHA256:test",
    })
    bundle = owner.inspect(
        path, require_signed=True, verifier=_verify, now=lambda: NOW,
    )

    replacement = AcceptanceEvidence.create(
        tmp_path / "replacement",
        acceptance_id="replacement-run",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="d" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        deployment_continuation=bundle,
        now=lambda: "2026-07-19T03:05:00Z",
        attestation_verifier=_verify,
    )
    assert replacement.deployment_mode == "adopt_existing"
    assert replacement.frontier == "P01"
    assert replacement.gate_attempt_count() == 0


def test_inconclusive_diagnostic_cannot_retain_deployment(tmp_path: Path) -> None:
    source, diagnostic = _source(
        tmp_path, diagnosed_attribution="inconclusive",
    )
    with pytest.raises(ValueError, match="does not prove"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_reconciled_environment_failure_can_retain_recovered_operation(
    tmp_path: Path,
) -> None:
    source, diagnostic = _source(
        tmp_path,
        diagnosed_attribution="environment_failure",
        bind_operation=False,
        recovered_operation_ids=("request-model-1",),
    )
    owner = _owner(tmp_path / "epochs")

    with pytest.raises(ValueError, match="every issued mutation"):
        owner.create(
            epoch_id="epoch-missing", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )

    path = owner.create(
        epoch_id="epoch-model", source=source, diagnostic=diagnostic,
        replacement=_replacement(),
        reconciliations=[_reconciliation("request-model-1")],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    record = owner.inspect(path)["record"]
    assert record["diagnosed_attribution"] == "environment_failure"
    assert [item["operation_id"] for item in record["reconciliations"]] == [
        "request-model-1"
    ]


def test_diagnostic_conclusion_rejects_tampered_proof(tmp_path: Path) -> None:
    source, diagnostic = _source(tmp_path)
    (diagnostic / "runner-proof.json").write_text('{"tampered":true}\n')

    with pytest.raises(ValueError, match="conclusion evidence"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_diagnostic_conclusion_rejects_secret_bearing_proof(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="forbidden field"):
        _source(tmp_path, proof_payload='{"password":"do-not-persist"}\n')


@pytest.mark.parametrize(
    ("owner", "field", "value"),
    [
        ("source", "acceptance_id", ""),
        ("source", "failed_gate", "X99"),
        ("cluster", "kube_context", ""),
    ],
)
def test_epoch_rejects_invalid_bound_source_identity(
    tmp_path: Path, owner: str, field: str, value: str,
) -> None:
    source, diagnostic = _source(tmp_path)
    epoch = _owner(tmp_path / "epochs").create(
        epoch_id="epoch-tampered", source=source, diagnostic=diagnostic,
        replacement=_replacement(), reconciliations=[_reconciliation()],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    record = DeploymentContinuation.inspect(epoch)["record"]
    record[owner][field] = value
    write_record(epoch, record)

    with pytest.raises(ValueError, match="record contract"):
        DeploymentContinuation.inspect(epoch)


def test_inspect_rejects_empty_signed_operation_inventory(tmp_path: Path) -> None:
    source, diagnostic = _source(tmp_path)
    epoch = _owner(tmp_path / "epochs").create(
        epoch_id="epoch-tampered", source=source, diagnostic=diagnostic,
        replacement=_replacement(), reconciliations=[_reconciliation()],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    record = DeploymentContinuation.inspect(epoch)["record"]
    record["source"]["issued_operation_ids"] = []
    record["reconciliations"] = []
    write_record(epoch, record)

    with pytest.raises(ValueError, match="record contract"):
        DeploymentContinuation.inspect(epoch)


@pytest.mark.parametrize(
    ("replacement", "reconciliation", "message"),
    [
        (_replacement("e" * 64), _reconciliation(), "product identity changed"),
        (_replacement(), _reconciliation(unknown_side_effects=True), "unknown side effects"),
        (_replacement(), _reconciliation(revision_identity={"fact": "value"}), "unprovable"),
        (_replacement(), None, "every issued mutation"),
    ],
)
def test_epoch_rejects_rebuild_required_conditions(
    tmp_path: Path,
    replacement: dict[str, object],
    reconciliation: dict | None,
    message: str,
) -> None:
    source, diagnostic = _source(tmp_path)
    with pytest.raises(ValueError, match=message):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=replacement,
            reconciliations=[] if reconciliation is None else [reconciliation],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_product_failure_cannot_be_reclassified_for_deployment_retention(
    tmp_path: Path,
) -> None:
    source, diagnostic = _source(
        tmp_path, failure_attribution="product_failure",
    )
    with pytest.raises(ValueError, match="Product failure"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_epoch_rejects_current_cluster_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, diagnostic = _source(tmp_path)
    monkeypatch.setattr(
        "aiops.acceptance.deployment_observation.observe_existing_deployment",
        lambda source, _commands, _archive, _checksums, **identity: _deployment_identity(
            source, identity["rendered_manifest_sha256"],
            identity["deployment_images_sha256"], cluster_identity_sha256="f" * 64,
        ),
    )
    with pytest.raises(ValueError, match="invalid or drifted"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_epoch_rejects_incomplete_operation_accounting(tmp_path: Path) -> None:
    source, diagnostic = _source(
        tmp_path, bind_operation=False, operation_accounting_complete=True,
    )
    with pytest.raises(ValueError, match="accounting is unprovable"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_epoch_rejects_rendered_manifest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, diagnostic = _source(tmp_path)
    monkeypatch.setattr(
        "aiops.acceptance.deployment_observation.observe_existing_deployment",
        lambda source, _commands, _archive, _checksums, **identity: _deployment_identity(
            source, "f" * 64, identity["deployment_images_sha256"],
        ),
    )
    with pytest.raises(ValueError, match="invalid or drifted"):
        _owner(tmp_path / "epochs").create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )


def test_epoch_requires_reconciliation_for_prior_gate_mutations(tmp_path: Path) -> None:
    source, diagnostic = _source(tmp_path, prior_operation_id="request-prior-1")
    owner = _owner(tmp_path / "epochs")
    with pytest.raises(ValueError, match="every issued mutation"):
        owner.create(
            epoch_id="epoch-missing", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
            release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
        )
    path = owner.create(
        epoch_id="epoch-complete", source=source, diagnostic=diagnostic,
        replacement=_replacement(),
        reconciliations=[_reconciliation(), _reconciliation("request-prior-1")],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    assert len(owner.inspect(path)["record"]["reconciliations"]) == 2


def test_epoch_owner_preserves_server_generation_only_diff_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, diagnostic = _source(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    (release / "manifest.yaml").write_bytes(b"kind: List\n")
    image = "registry.example.test/aiops/gateway@sha256:" + "1" * 64

    monkeypatch.setattr(
        "aiops.acceptance.deployment_observation.observe_existing_deployment",
        REAL_OBSERVE_DEPLOYMENT,
    )
    monkeypatch.setattr(
        "aiops.acceptance.package_install.PackageInstallRunner.prepare_release",
        lambda _self, _archive, _checksums, *, work_dir: release,
    )
    monkeypatch.setattr(
        "aiops.acceptance.deployment_observation.release_image_inventory",
        lambda _release: {image},
    )
    monkeypatch.setattr(
        "aiops.acceptance.cluster_install.ClusterInstallRunner.observe_existing",
        lambda _self, _release: {
            "cluster_identity_sha256": CLUSTER_IDENTITY,
            "manifest_diff": CommandResult(
                ("kubectl", "diff"), 1, "generation: 2 -> 3\n", "", 0.1,
            ),
            "server_generation_only": True,
            "objects": {"deployments": []},
            "configuration": {"configmaps": []},
            "bootstrap": {"completion_marker": "verified"},
            "workloads": {"ready": True},
            "events": [],
        },
    )
    owner = DeploymentContinuation(
        tmp_path / "epochs", commands=object(), now=lambda: NOW,  # type: ignore[arg-type]
    )
    path = owner.create(
        epoch_id="epoch-observed", source=source, diagnostic=diagnostic,
        replacement=_replacement(), reconciliations=[_reconciliation()],
        release_archive=UNUSED_RELEASE, release_checksums=UNUSED_RELEASE,
    )
    identity = owner.inspect(path)["record"]["deployment_identity"]
    assert identity["manifest_diff_exit_code"] == 1
    assert identity["manifest_diff_server_generation_only"] is True
    assert identity["manifest_diff_sha256"] == hashlib.sha256(
        b"generation: 2 -> 3\n"
    ).hexdigest()
    assert identity["cluster_identity_sha256"] == CLUSTER_IDENTITY
