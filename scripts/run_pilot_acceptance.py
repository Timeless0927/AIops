#!/usr/bin/env python3
"""Run A01 Pilot acceptance gates without persisting credentials."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops.acceptance.adapters import HttpsProfileProbe, OpenSshSigner, PlaywrightBrowser
from aiops.acceptance.cluster_install import ClusterInstallRunner
from aiops.acceptance.command import SubprocessCommands
from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.http import GatewaySession
from aiops.acceptance.connector_gate import ConnectorGateRunner
from aiops.acceptance.model_gate import ModelGateRunner, ModelInputs
from aiops.acceptance.notification_gate import (
    NotificationGateRunner,
    NotificationInputs,
    SUPPORTED_NOTIFICATION_PROVIDERS,
)
from aiops.acceptance.observability_gate import ObservabilityGateRunner
from aiops.acceptance.package_install import (
    PackageInstallRunner,
    release_connector_identity,
    sha256,
)
from aiops.acceptance.platform_status_gates import PlatformStatusGateRunner
from aiops.acceptance.telemetry import KubernetesTelemetryProbe
from aiops.acceptance.web_gates import WebGateRunner


ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_NOTES = {
    "P03": "clean non-production Cluster, 32Gi capacity and enforced NetworkPolicy confirmed",
    "I05": "bootstrap-password first login completed in a real browser",
    "S04": "real test Notification message received by the declared recipient",
    "S05": "Connector credential was displayed once and is not retrievable",
    "V05": "exact dry-run diff, unavailable rollback and controlled rollout target confirmed",
}
ATTESTATION_ROLES = {
    "P03": "platform_operator",
    "I05": "platform_administrator",
    "S04": "platform_administrator",
    "S05": "platform_operator",
    "V05": "sre",
}


def _require(result, action: str):
    if result.exit_code != 0:
        raise RuntimeError(f"{action} failed: {result.stderr.strip() or 'no detail'}")
    return result


def _cluster_identity(commands: SubprocessCommands) -> tuple[str, str]:
    context = _require(
        commands.run(["kubectl", "config", "current-context"], timeout=15),
        "read kube context",
    ).stdout.strip()
    config = json.loads(
        _require(
            commands.run(["kubectl", "config", "view", "--minify", "-o", "json"], timeout=15),
            "read cluster identity",
        ).stdout
    )
    clusters = config.get("clusters", [])
    if len(clusters) != 1:
        raise RuntimeError("current kube context must resolve exactly one Cluster")
    cluster = clusters[0].get("cluster", {})
    identity = {
        "server": cluster.get("server"),
        "certificate_authority_data": cluster.get("certificate-authority-data", ""),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return context, digest


def _login(base_url: str, username: str, password: str) -> GatewaySession:
    session = GatewaySession(base_url)
    response = session.request(
        "POST",
        "/auth/login",
        body={"username": username, "password": password, "session_mode": "cookie"},
        csrf=False,
    )
    if response.status != 200:
        raise RuntimeError(f"login for {username!r} returned HTTP {response.status}")
    return session


def _attest(evidence: AcceptanceEvidence, gate_id: str, actor: str, key_path: Path) -> None:
    statement = evidence.attestation_statement(
        actor=actor,
        role=ATTESTATION_ROLES[gate_id],
        gate_ids=[gate_id],
        conclusion="passed",
        note=ATTESTATION_NOTES[gate_id],
    )
    signer = OpenSshSigner()
    signed = signer.sign(statement, key_path=key_path)
    signer.verify(
        statement,
        signature=signed["signature"],
        public_key=signed["public_key"],
        identity=actor,
    )
    evidence.append_attestation(statement, **signed)


def _verify_attestation(item: dict) -> None:
    signer = OpenSshSigner()
    statement = item["statement"]
    signer.verify(
        statement,
        signature=item["signature"],
        public_key=item["public_key"],
        identity=statement["actor"],
    )
    if signer.fingerprint(item["public_key"]) != item["fingerprint"]:
        raise RuntimeError("attestation public-key fingerprint mismatch")


def _open_evidence(path: Path) -> AcceptanceEvidence:
    return AcceptanceEvidence.open(path, attestation_verifier=_verify_attestation)


def _interactive_attest(evidence: AcceptanceEvidence, gate_id: str) -> None:
    actor = input(f"{gate_id} attestation actor: ").strip()
    key_path = Path(input(f"{gate_id} OpenSSH private key path: ").strip()).expanduser()
    _attest(evidence, gate_id, actor, key_path)


def _feishu_inputs() -> dict:
    return {"webhook_url": getpass.getpass("Feishu webhook URL: ")}


def _dingtalk_inputs() -> dict:
    return {
        "webhook_url": getpass.getpass("DingTalk webhook URL: "),
        "signing_secret": getpass.getpass("DingTalk signing secret: "),
    }


def _smtp_inputs() -> dict:
    return {
        "host": input("SMTP host: ").strip(),
        "port": int(input("SMTP port: ").strip()),
        "username": getpass.getpass("SMTP username: "),
        "password": getpass.getpass("SMTP password: "),
        "from_address": getpass.getpass("SMTP from address: "),
        "to_addresses": [
            item.strip()
            for item in getpass.getpass("SMTP recipient addresses (comma separated): ").split(",")
            if item.strip()
        ],
        "tls_mode": input("SMTP TLS mode [starttls/ssl]: ").strip(),
    }


_NOTIFICATION_INPUT_READERS = {
    "feishu": _feishu_inputs,
    "dingtalk": _dingtalk_inputs,
    "smtp": _smtp_inputs,
}


def _notification_inputs() -> NotificationInputs:
    provider = input(
        f"Notification provider [{'|'.join(SUPPORTED_NOTIFICATION_PROVIDERS)}]: "
    ).strip()
    try:
        config = _NOTIFICATION_INPUT_READERS[provider]()
    except KeyError as exc:
        raise ValueError("unsupported Notification provider") from exc
    return NotificationInputs(provider, config)


def cmd_init(args: argparse.Namespace) -> None:
    commands = SubprocessCommands()
    context, identity = _cluster_identity(commands)
    version = args.archive.name.removeprefix("aiops-pilot-").removesuffix(".tar.gz")
    acceptance_id = args.acceptance_id or (
        f"{version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    evidence = AcceptanceEvidence.create(
        args.output,
        acceptance_id=acceptance_id,
        release_version=version,
        release_sha256=sha256(args.archive),
        kube_context=context,
        cluster_identity_sha256=identity,
        access_profile=args.access_profile,
    )
    print(evidence.root)


def cmd_attest(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    _attest(evidence, args.gate, args.actor, args.key)
    print(evidence.attestation_path)


def cmd_package(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    runner = PackageInstallRunner(evidence=evidence, commands=SubprocessCommands())
    runner.run_p01(args.archive, args.checksums, work_dir=args.work_dir)
    runner.run_p02(args.source_root)


def _prepared(args: argparse.Namespace, evidence: AcceptanceEvidence, commands: SubprocessCommands):
    return PackageInstallRunner(evidence=evidence, commands=commands).prepare_release(
        args.archive, args.checksums, work_dir=args.work_dir
    )


def cmd_install(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    commands = SubprocessCommands()
    release = _prepared(args, evidence, commands)
    runner = ClusterInstallRunner(evidence=evidence, commands=commands)
    runner.run_p03(release)
    runner.run_i01(release)
    runner.run_i02(release)


def cmd_web(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    admin_username = input("Platform Administrator username [admin]: ").strip() or "admin"
    admin_password = getpass.getpass("Platform Administrator password: ")
    user_username = input("Ordinary User username: ").strip()
    user_password = getpass.getpass("Ordinary User password: ")
    commands = SubprocessCommands()
    browser = PlaywrightBrowser(commands=commands, source_root=args.source_root)
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=GatewaySession(args.base_url),
        session_factory=lambda: GatewaySession(args.base_url),
        browser=browser,
    )
    runner.run_i03(
        args.base_url,
        admin_username=admin_username,
        admin_password=admin_password,
    )
    https_identity = None
    if evidence.access_profile == "https_ingress" and args.https_base_url:
        https_identity = HttpsProfileProbe(commands).probe(
            args.https_base_url,
            ingress=args.https_ingress or "",
            http_url=args.https_http_url,
            require_redirect=args.https_require_redirect,
        )
    runner.run_i04(args.https_base_url, https_identity)
    runner.run_i05(
        admin_username=admin_username,
        admin_password=admin_password,
        user_username=user_username,
        user_password=user_password,
    )


def cmd_setup(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    admin_username = input("Platform Administrator username [admin]: ").strip() or "admin"
    admin_password = getpass.getpass("Platform Administrator password: ")
    user_username = input("Ordinary User username: ").strip()
    user_password = getpass.getpass("Ordinary User password: ")
    admin = _login(args.base_url, admin_username, admin_password)
    user = _login(args.base_url, user_username, user_password)
    commands = SubprocessCommands()
    release = _prepared(args, evidence, commands)
    browser = PlaywrightBrowser(commands=commands, source_root=args.source_root)
    status = PlatformStatusGateRunner(
        evidence=evidence,
        admin=admin,
        relogin=lambda: _login(args.base_url, admin_username, admin_password),
        stale_admin=lambda: _login(args.base_url, admin_username, admin_password),
        user=user,
        browser=browser,
        base_url=args.base_url,
    )
    status.run_s01(admin_username=admin_username, admin_password=admin_password)
    status.run_s02(admin_password=admin_password)
    model = ModelInputs(
        endpoint=input("OpenAI-compatible Model endpoint: ").strip(),
        endpoint_scope=input("Model endpoint scope [external/cluster_internal]: ").strip(),
        model=input("Model name: ").strip(),
        timeout_seconds=int(input("Model timeout seconds [5-120]: ").strip()),
        api_key=getpass.getpass("Model API key: "),
    )
    ModelGateRunner(evidence=evidence, admin=admin).run_s03(
        model, admin_password=admin_password
    )
    notification = _notification_inputs()

    def confirm_receipt(_delivery_id: str) -> None:
        input("Confirm the real test message is visible, then press Enter: ")
        _interactive_attest(evidence, "S04")

    NotificationGateRunner(evidence=evidence, admin=admin).run_s04(
        notification, admin_password=admin_password, confirm_receipt=confirm_receipt
    )

    def confirm_one_time() -> None:
        input("Confirm Connector credential one-time handling, then press Enter: ")
        _interactive_attest(evidence, "S05")

    connector_id, cluster_id = release_connector_identity(release)
    ConnectorGateRunner(evidence=evidence, admin=admin, commands=commands).run_s05(
        admin_password=admin_password,
        connector_id=connector_id,
        cluster_id=cluster_id,
        confirm_one_time=confirm_one_time,
    )
    ObservabilityGateRunner(
        evidence=evidence, telemetry=KubernetesTelemetryProbe(commands)
    ).run_s06()


def cmd_finalize(args: argparse.Namespace) -> None:
    evidence = _open_evidence(args.acceptance)
    print(evidence.finalize())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--archive", type=Path, required=True)
    initialize.add_argument("--output", type=Path, default=Path("acceptance"))
    initialize.add_argument("--acceptance-id")
    initialize.add_argument(
        "--access-profile", choices=("http_nodeport", "https_ingress"), default="http_nodeport"
    )
    initialize.set_defaults(func=cmd_init)
    attest = sub.add_parser("attest")
    attest.add_argument("--acceptance", type=Path, required=True)
    attest.add_argument("--gate", choices=tuple(ATTESTATION_NOTES), required=True)
    attest.add_argument("--actor", required=True)
    attest.add_argument("--key", type=Path, required=True)
    attest.set_defaults(func=cmd_attest)
    for name, handler in (("package", cmd_package), ("install", cmd_install)):
        command = sub.add_parser(name)
        command.add_argument("--acceptance", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--checksums", type=Path, required=True)
        command.add_argument("--work-dir", type=Path, required=True)
        if name == "package":
            command.add_argument("--source-root", type=Path, default=ROOT)
        command.set_defaults(func=handler)
    web = sub.add_parser("web")
    web.add_argument("--acceptance", type=Path, required=True)
    web.add_argument("--base-url", required=True)
    web.add_argument("--https-base-url")
    web.add_argument("--https-ingress", help="namespace/name for declared HTTPS profile")
    web.add_argument("--https-http-url")
    web.add_argument("--https-require-redirect", action="store_true")
    web.add_argument("--source-root", type=Path, default=ROOT)
    web.set_defaults(func=cmd_web)
    setup = sub.add_parser("setup")
    setup.add_argument("--acceptance", type=Path, required=True)
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--source-root", type=Path, default=ROOT)
    setup.add_argument("--archive", type=Path, required=True)
    setup.add_argument("--checksums", type=Path, required=True)
    setup.add_argument("--work-dir", type=Path, required=True)
    setup.set_defaults(func=cmd_setup)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--acceptance", type=Path, required=True)
    finalize.set_defaults(func=cmd_finalize)
    return result


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
