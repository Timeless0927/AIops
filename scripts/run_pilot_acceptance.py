#!/usr/bin/env python3
"""Qualify or adopt one deployment, then drive one Format v4 frontier per invocation."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops.acceptance.adapters import OpenSshSigner
from aiops.acceptance.command import SubprocessCommands
from aiops.acceptance.cluster_identity import KubernetesClusterIdentitySource
from aiops.acceptance.conductor import AcceptanceConductor
from aiops.acceptance.credentials import RunCredentialStore
from aiops.acceptance.deployment_continuation import (
    DeploymentContinuation,
    conclude_diagnostic_bundle,
    create_diagnostic_bundle,
    replacement_identity,
)
from aiops.acceptance.deployment_observation import replacement_release_paths
from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.environment_qualification import EnvironmentQualification
from aiops.acceptance import evaluator_successor
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from aiops.acceptance.gate_reuse import reuse_gate
from aiops.acceptance.package_install import sha256
from aiops.acceptance.promotion import PromotionDecision
from aiops.acceptance.runtime import AcceptanceRuntime, RESUMABLE_GATES


ROOT = Path(__file__).resolve().parents[1]
_ROLES = ("platform_operator", "platform_administrator", "sre")
_FIXED_ATTESTATIONS = {
    ("I05", "platform_administrator"): (
        "bootstrap-password first login completed in a real browser"
    ),
    ("S05", "platform_operator"): (
        "Connector credential was displayed once and is not retrievable"
    ),
    ("V04", "sre"): "exact diff confirmed",
    ("V05", "sre"): "exact diff confirmed",
}
_REVIEW_ATTESTATIONS = {
    ("S04", "platform_administrator"): (
        "receipt-review.json", "notification_receipt_sha256"
    ),
    ("V07", "sre"): ("report-review.json", "report_review_sha256"),
    ("R01", "platform_operator"): ("recovery-review.json", "recovery_review_sha256"),
    ("R02", "platform_operator"): ("recovery-review.json", "recovery_review_sha256"),
    ("R03", "platform_operator"): ("recovery-review.json", "recovery_review_sha256"),
    ("R03", "sre"): ("approval-review.json", "approval_review_sha256"),
    ("R04", "platform_operator"): ("recovery-review.json", "recovery_review_sha256"),
    ("R06", "sre"): ("approval-review.json", "approval_review_sha256"),
    ("R06", "platform_operator"): ("drift-review.json", "drift_review_sha256"),
    ("V08", "platform_administrator"): (
        "destination-receipt-review.json", "notification_receipt_sha256"
    ),
    ("C03", "platform_operator"): ("manifest-summary.json", "manifest_summary_sha256"),
    ("C03", "platform_administrator"): (
        "manifest-summary.json", "manifest_summary_sha256"
    ),
    ("C03", "sre"): ("manifest-summary.json", "manifest_summary_sha256"),
}


def _verify_signed(item: dict[str, Any]) -> None:
    signer = OpenSshSigner()
    statement = item["statement"]
    signer.verify(
        statement, signature=item["signature"], public_key=item["public_key"],
        identity=str(statement.get("actor") or "release-maintainer"),
    )
    if signer.fingerprint(item["public_key"]) != item["fingerprint"]:
        raise RuntimeError("signed statement public-key fingerprint mismatch")


def _open(path: Path) -> AcceptanceEvidence:
    return AcceptanceEvidence.open(path, attestation_verifier=_verify_signed)


def _inspect_handoff(path: Path) -> dict[str, Any]:
    if evaluator_successor.is_bundle(path):
        return evaluator_successor.inspect(path)
    return DeploymentContinuation.inspect(
        path, require_signed=True, verifier=_verify_signed,
        now=lambda: datetime.now(timezone.utc),
    )


def _sign(statement: dict[str, Any], key_path: Path) -> dict[str, str]:
    signer = OpenSshSigner()
    signed = signer.sign(statement, key_path=key_path)
    signer.verify(
        statement, signature=signed["signature"], public_key=signed["public_key"],
        identity=str(statement.get("actor") or "release-maintainer"),
    )
    return signed


def _review_artifact(evidence: AcceptanceEvidence, gate_id: str, name: str):
    if evidence.open_gate == gate_id:
        matches = [
            item for item in evidence.resume_gate(gate_id).artifacts
            if item.path.name == name
        ]
        if len(matches) != 1:
            raise ValueError(f"{gate_id} requires one {name} review artifact")
        return matches[0]
    return evidence.passed_artifact(gate_id, name)


def _attestation_note(
    evidence: AcceptanceEvidence, gate_id: str, role: str,
) -> str:
    fixed = _FIXED_ATTESTATIONS.get((gate_id, role))
    if fixed is not None:
        return fixed
    if gate_id == "V08" and role == "sre":
        approval = _review_artifact(evidence, gate_id, "approval-review.json")
        approval_note = f"approval_review_sha256={approval.sha256}"
        if not any(
            item.get("statement", {}).get("note") == approval_note
            for item in evidence.attestations_for(gate_id, role=role)
        ):
            return approval_note
        review = _review_artifact(evidence, gate_id, "report-review.json")
        return f"report_review_sha256={review.sha256}"
    review = _REVIEW_ATTESTATIONS.get((gate_id, role))
    if review is None:
        raise ValueError(f"{gate_id} does not require a {role} attestation")
    name, prefix = review
    return f"{prefix}={_review_artifact(evidence, gate_id, name).sha256}"


def _append_attestation(
    evidence: AcceptanceEvidence,
    *,
    gate_id: str,
    role: str,
    actor: str,
    key_path: Path,
) -> Path:
    statement = evidence.attestation_statement(
        actor=actor, role=role, gate_ids=[gate_id], conclusion="passed",
        note=_attestation_note(evidence, gate_id, role),
    )
    evidence.append_attestation(statement, **_sign(statement, key_path))
    return evidence.attestation_path


def _interactive_attest(evidence: AcceptanceEvidence, gate_id: str, role: str) -> None:
    input(f"Review {gate_id} evidence as {role}; press Enter to sign: ")
    actor = input("Attestation actor: ").strip()
    key = Path(input("OpenSSH private key path: ").strip()).expanduser()
    _append_attestation(
        evidence, gate_id=gate_id, role=role, actor=actor, key_path=key,
    )


def _runtime(args: argparse.Namespace, evidence: AcceptanceEvidence) -> AcceptanceRuntime:
    return AcceptanceRuntime.from_file(
        evidence=evidence, config_path=args.config, source_root=ROOT,
        credential_store=args.credential_store, admission_verifier=_verify_signed,
        attest=lambda gate_id, role: _interactive_attest(evidence, gate_id, role),
    )


def _conductor(args: argparse.Namespace) -> AcceptanceConductor:
    evidence = _open(args.acceptance)
    runtime = _runtime(args, evidence)
    advance = {
        gate_id: (lambda gate_id=gate_id: runtime.advance(gate_id))
        for gate_id in GATE_SEQUENCE
    }
    resume = {
        gate_id: (lambda gate_id=gate_id: runtime.resume(gate_id))
        for gate_id in RESUMABLE_GATES
    }
    return AcceptanceConductor(
        evidence, advance_commands=advance, resume_commands=resume,
    )


def cmd_init(args: argparse.Namespace) -> None:
    context, identity = KubernetesClusterIdentitySource(SubprocessCommands()).read()
    version = args.archive.name.removeprefix("aiops-pilot-").removesuffix(".tar.gz")
    acceptance_id = args.acceptance_id or (
        f"{version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    precondition = (
        {"environment_qualification": EnvironmentQualification.inspect(
            args.environment_qualification, require_signed=True,
            verifier=_verify_signed, now=lambda: datetime.now(timezone.utc),
        )}
        if args.environment_qualification is not None
        else {"deployment_continuation": _inspect_handoff(
            args.deployment_continuation,
        )}
    )
    evidence = AcceptanceEvidence.create(
        args.output, acceptance_id=acceptance_id, release_version=version,
        release_sha256=sha256(args.archive),
        acceptance_tool_sha256=sha256(args.acceptance_tool),
        gate_contract_revision=GATE_CONTRACT_REVISION, kube_context=context,
        cluster_identity_sha256=identity, access_profile=args.access_profile,
        **precondition,
        attestation_verifier=_verify_signed,
    )
    print(evidence.root)


def cmd_qualification_create(args: argparse.Namespace) -> None:
    context, identity = KubernetesClusterIdentitySource(SubprocessCommands()).read()
    options = ({"new_id": lambda: args.qualification_id} if args.qualification_id else {})
    qualification = EnvironmentQualification(
        args.output, commands=SubprocessCommands(), **options,
    )
    print(qualification.qualify(
        args.freeze, kube_context=context, cluster_identity_sha256=identity,
        access_profile=args.access_profile, ttl_seconds=args.ttl_seconds,
    ))


def cmd_qualification_inspect(args: argparse.Namespace) -> None:
    print(json.dumps(EnvironmentQualification.inspect(args.qualification), sort_keys=True))


def cmd_qualification_resume(args: argparse.Namespace) -> None:
    qualification = EnvironmentQualification(
        args.qualification.parent, commands=SubprocessCommands(),
    )
    print(json.dumps(qualification.resume_cleanup(args.qualification), sort_keys=True))


def cmd_qualification_attest(args: argparse.Namespace) -> None:
    qualification = EnvironmentQualification(
        args.qualification.parent, commands=SubprocessCommands(),
    )
    statement = qualification.attestation_statement(
        args.qualification, actor=args.actor, note=args.note,
    )
    qualification.attach_attestation(
        args.qualification, {"statement": statement, **_sign(statement, args.key)},
    )
    print(json.dumps(qualification.inspect(
        args.qualification, require_signed=True, verifier=_verify_signed,
    ), sort_keys=True))


def cmd_continuation_create(args: argparse.Namespace) -> None:
    epoch_id = args.epoch_id or (
        "continuation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    try:
        reconciliations = json.loads(args.reconciliations.read_text(encoding="utf-8"))
        gate_reuse_plan = json.loads(args.gate_reuse_plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("continuation inputs are unreadable") from exc
    if any(
        not isinstance(value, list)
        or any(not isinstance(item, dict) for item in value)
        for value in (reconciliations, gate_reuse_plan)
    ):
        raise ValueError("continuation inputs must be JSON arrays")
    archive, checksums = replacement_release_paths(args.freeze)
    owner = DeploymentContinuation(args.output, commands=SubprocessCommands())
    print(owner.create(
        epoch_id=epoch_id, source=_open(args.source_acceptance),
        diagnostic=args.diagnostic, replacement=replacement_identity(args.freeze),
        reconciliations=reconciliations,
        gate_reuse_plan=gate_reuse_plan,
        release_archive=archive, release_checksums=checksums,
        ttl_seconds=args.ttl_seconds,
    ))


def cmd_diagnostic_create(args: argparse.Namespace) -> None:
    print(create_diagnostic_bundle(
        _open(args.acceptance), args.output, diagnostic_id=args.diagnostic_id,
    ))


def cmd_diagnostic_conclude(args: argparse.Namespace) -> None:
    print(conclude_diagnostic_bundle(
        args.diagnostic,
        diagnosed_attribution=args.failure_attribution,
        conclusion_note=args.note,
        evidence=args.evidence,
        recovered_operation_ids=args.recovered_operation_id,
        operation_accounting_complete=args.operation_accounting_complete,
    ))


def cmd_continuation_inspect(args: argparse.Namespace) -> None:
    print(json.dumps(DeploymentContinuation.inspect(args.continuation), sort_keys=True))


def cmd_continuation_attest(args: argparse.Namespace) -> None:
    owner = DeploymentContinuation(args.continuation.parent)
    statement = owner.attestation_statement(
        args.continuation, actor=args.actor, note=args.note,
    )
    owner.attach_attestation(
        args.continuation, {"statement": statement, **_sign(statement, args.key)},
    )
    print(json.dumps(owner.inspect(
        args.continuation, require_signed=True, verifier=_verify_signed,
    ), sort_keys=True))


def cmd_evaluator_successor_create(args: argparse.Namespace) -> None:
    print(evaluator_successor.create(
        _open(args.source_acceptance),
        diagnostic=args.diagnostic,
        acceptance_tool=args.acceptance_tool,
        output=args.output,
    ))


def cmd_evaluator_successor_inspect(args: argparse.Namespace) -> None:
    print(json.dumps(evaluator_successor.inspect(args.successor), sort_keys=True))


def cmd_gate_reuse_apply(args: argparse.Namespace) -> None:
    continuation = _inspect_handoff(args.deployment_continuation)
    print(json.dumps(reuse_gate(
        source=_open(args.source_acceptance),
        target=_open(args.acceptance),
        continuation=continuation,
        now=lambda: datetime.now(timezone.utc),
        verifier=_verify_signed,
    ), sort_keys=True))


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(_open(args.acceptance).status(), sort_keys=True))


def cmd_advance(args: argparse.Namespace) -> None:
    print(json.dumps(_conductor(args).advance(), default=str, sort_keys=True))


def cmd_resume(args: argparse.Namespace) -> None:
    print(json.dumps(_conductor(args).resume(), default=str, sort_keys=True))


def cmd_attest(args: argparse.Namespace) -> None:
    path = _append_attestation(
        _open(args.acceptance), gate_id=args.gate, role=args.role,
        actor=args.actor, key_path=args.key,
    )
    print(path)


def cmd_evaluate(args: argparse.Namespace) -> None:
    print(json.dumps(_open(args.acceptance).evaluate(), sort_keys=True))


def cmd_correct(args: argparse.Namespace) -> None:
    evidence = _open(args.acceptance)
    if args.gate != "S01":
        raise ValueError("this tool revision only supports S01 evaluator correction")
    print(json.dumps(evidence.correct_s01(
        acceptance_tool=args.acceptance_tool,
        diagnostic=args.diagnostic,
        reason=args.reason,
    ), sort_keys=True))


def cmd_decide(args: argparse.Namespace) -> None:
    evidence = _open(args.acceptance)
    decision = PromotionDecision(evidence)
    statement = decision.statement(
        actor=args.actor, decision=args.decision, note=args.note,
    )
    print(json.dumps(decision.record(statement, **_sign(statement, args.key)), sort_keys=True))


def cmd_seal(args: argparse.Namespace) -> None:
    evidence = _open(args.acceptance)
    path = evidence.seal()
    if args.credential_store is not None:
        RunCredentialStore(
            args.credential_store, workspace=ROOT, evidence_root=evidence.root,
        ).open().cleanup_if_terminal(evidence)
    print(path)


def cmd_credential_create(args: argparse.Namespace) -> None:
    evidence = _open(args.acceptance)
    store = RunCredentialStore(
        args.store, workspace=ROOT, evidence_root=evidence.root,
    ).create()
    store.generate_user_password("ordinary-user-password")
    store.generate_user_password("sre-password")
    print(store.root)


def cmd_credential_import(args: argparse.Namespace) -> None:
    evidence = _open(args.acceptance)
    store = RunCredentialStore(
        args.store, workspace=ROOT, evidence_root=evidence.root,
    ).open()
    store.import_secret(args.name, args.source)
    print(args.name)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--archive", type=Path, required=True)
    initialize.add_argument("--acceptance-tool", type=Path, required=True)
    initialize.add_argument("--output", type=Path, default=Path("acceptance"))
    initialize.add_argument("--acceptance-id")
    precondition = initialize.add_mutually_exclusive_group(required=True)
    precondition.add_argument("--environment-qualification", type=Path)
    precondition.add_argument("--deployment-continuation", type=Path)
    initialize.add_argument(
        "--access-profile", choices=("http_nodeport", "https_ingress"),
        default="http_nodeport",
    )
    initialize.set_defaults(func=cmd_init)
    qualification = sub.add_parser("qualification")
    qualification_sub = qualification.add_subparsers(
        dest="qualification_command", required=True,
    )
    qualification_create = qualification_sub.add_parser("create")
    qualification_create.add_argument("--freeze", type=Path, required=True)
    qualification_create.add_argument("--output", type=Path, required=True)
    qualification_create.add_argument("--qualification-id")
    qualification_create.add_argument("--ttl-seconds", type=int, default=3600)
    qualification_create.add_argument(
        "--access-profile", choices=("http_nodeport", "https_ingress"),
        default="http_nodeport",
    )
    qualification_create.set_defaults(func=cmd_qualification_create)
    for name, handler in (
        ("inspect", cmd_qualification_inspect), ("resume", cmd_qualification_resume),
    ):
        command = qualification_sub.add_parser(name)
        command.add_argument("--qualification", type=Path, required=True)
        command.set_defaults(func=handler)
    qualification_attest = qualification_sub.add_parser("attest")
    qualification_attest.add_argument("--qualification", type=Path, required=True)
    qualification_attest.add_argument("--actor", required=True)
    qualification_attest.add_argument("--note", required=True)
    qualification_attest.add_argument("--key", type=Path, required=True)
    qualification_attest.set_defaults(func=cmd_qualification_attest)
    diagnostic = sub.add_parser("diagnostic")
    diagnostic_sub = diagnostic.add_subparsers(
        dest="diagnostic_command", required=True,
    )
    diagnostic_create = diagnostic_sub.add_parser("create")
    diagnostic_create.add_argument("--acceptance", type=Path, required=True)
    diagnostic_create.add_argument("--output", type=Path, required=True)
    diagnostic_create.add_argument("--diagnostic-id", required=True)
    diagnostic_create.set_defaults(func=cmd_diagnostic_create)
    diagnostic_conclude = diagnostic_sub.add_parser("conclude")
    diagnostic_conclude.add_argument("--diagnostic", type=Path, required=True)
    diagnostic_conclude.add_argument(
        "--failure-attribution",
        choices=(
            "product_failure", "tool_failure", "environment_failure", "inconclusive",
        ),
        required=True,
    )
    diagnostic_conclude.add_argument("--note", required=True)
    diagnostic_conclude.add_argument(
        "--evidence", type=Path, action="append", required=True,
    )
    diagnostic_conclude.add_argument(
        "--recovered-operation-id", action="append", default=[],
    )
    diagnostic_conclude.add_argument(
        "--operation-accounting-complete", action="store_true",
    )
    diagnostic_conclude.set_defaults(func=cmd_diagnostic_conclude)
    continuation = sub.add_parser("continuation")
    continuation_sub = continuation.add_subparsers(
        dest="continuation_command", required=True,
    )
    continuation_create = continuation_sub.add_parser("create")
    continuation_create.add_argument("--source-acceptance", type=Path, required=True)
    continuation_create.add_argument("--diagnostic", type=Path, required=True)
    continuation_create.add_argument("--freeze", type=Path, required=True)
    continuation_create.add_argument("--reconciliations", type=Path, required=True)
    continuation_create.add_argument("--gate-reuse-plan", type=Path, required=True)
    continuation_create.add_argument("--output", type=Path, required=True)
    continuation_create.add_argument("--epoch-id")
    continuation_create.add_argument("--ttl-seconds", type=int, default=3600)
    continuation_create.set_defaults(func=cmd_continuation_create)
    continuation_inspect = continuation_sub.add_parser("inspect")
    continuation_inspect.add_argument("--continuation", type=Path, required=True)
    continuation_inspect.set_defaults(func=cmd_continuation_inspect)
    continuation_attest = continuation_sub.add_parser("attest")
    continuation_attest.add_argument("--continuation", type=Path, required=True)
    continuation_attest.add_argument("--actor", required=True)
    continuation_attest.add_argument("--note", required=True)
    continuation_attest.add_argument("--key", type=Path, required=True)
    continuation_attest.set_defaults(func=cmd_continuation_attest)
    successor = sub.add_parser("evaluator-successor")
    successor_sub = successor.add_subparsers(
        dest="evaluator_successor_command", required=True,
    )
    successor_create = successor_sub.add_parser("create")
    successor_create.add_argument("--source-acceptance", type=Path, required=True)
    successor_create.add_argument("--diagnostic", type=Path, required=True)
    successor_create.add_argument("--acceptance-tool", type=Path, required=True)
    successor_create.add_argument("--output", type=Path, required=True)
    successor_create.set_defaults(func=cmd_evaluator_successor_create)
    successor_inspect = successor_sub.add_parser("inspect")
    successor_inspect.add_argument("--successor", type=Path, required=True)
    successor_inspect.set_defaults(func=cmd_evaluator_successor_inspect)
    gate_reuse = sub.add_parser("gate-reuse")
    gate_reuse_sub = gate_reuse.add_subparsers(
        dest="gate_reuse_command", required=True,
    )
    gate_reuse_apply = gate_reuse_sub.add_parser("apply")
    gate_reuse_apply.add_argument(
        "--deployment-continuation", type=Path, required=True,
    )
    gate_reuse_apply.add_argument("--source-acceptance", type=Path, required=True)
    gate_reuse_apply.add_argument("--acceptance", type=Path, required=True)
    gate_reuse_apply.set_defaults(func=cmd_gate_reuse_apply)
    status = sub.add_parser("status")
    status.add_argument("--acceptance", type=Path, required=True)
    status.set_defaults(func=cmd_status)
    for name, handler in (("advance", cmd_advance), ("resume", cmd_resume)):
        command = sub.add_parser(name)
        command.add_argument("--acceptance", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--credential-store", type=Path)
        command.set_defaults(func=handler)
    attest = sub.add_parser("attest")
    attest.add_argument("--acceptance", type=Path, required=True)
    attest.add_argument("--gate", choices=GATE_SEQUENCE, required=True)
    attest.add_argument("--role", choices=_ROLES, required=True)
    attest.add_argument("--actor", required=True)
    attest.add_argument("--key", type=Path, required=True)
    attest.set_defaults(func=cmd_attest)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--acceptance", type=Path, required=True)
    evaluate.set_defaults(func=cmd_evaluate)
    correct = sub.add_parser("correct")
    correct.add_argument("--acceptance", type=Path, required=True)
    correct.add_argument("--gate", choices=("S01",), required=True)
    correct.add_argument("--acceptance-tool", type=Path, required=True)
    correct.add_argument("--diagnostic", type=Path, required=True)
    correct.add_argument("--reason", required=True)
    correct.set_defaults(func=cmd_correct)
    decide = sub.add_parser("decide")
    decide.add_argument("--acceptance", type=Path, required=True)
    decide.add_argument("--decision", choices=("promote", "no_promote"), required=True)
    decide.add_argument("--actor", required=True)
    decide.add_argument("--note", required=True)
    decide.add_argument("--key", type=Path, required=True)
    decide.set_defaults(func=cmd_decide)
    seal = sub.add_parser("seal")
    seal.add_argument("--acceptance", type=Path, required=True)
    seal.add_argument("--credential-store", type=Path)
    seal.set_defaults(func=cmd_seal)
    credentials = sub.add_parser("credential-store")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    create = credential_sub.add_parser("create")
    create.add_argument("--acceptance", type=Path, required=True)
    create.add_argument("--store", type=Path, required=True)
    create.set_defaults(func=cmd_credential_create)
    import_secret = credential_sub.add_parser("import")
    import_secret.add_argument("--acceptance", type=Path, required=True)
    import_secret.add_argument("--store", type=Path, required=True)
    import_secret.add_argument(
        "--name", choices=("model-api-key", "notification-config"), required=True,
    )
    import_secret.add_argument("--source", type=Path, required=True)
    import_secret.set_defaults(func=cmd_credential_import)
    return result


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
