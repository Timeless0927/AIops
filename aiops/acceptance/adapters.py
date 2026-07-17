"""Concrete browser and OpenSSH adapters for the acceptance CLI."""

from __future__ import annotations

import json
import hashlib
import socket
import ssl
import subprocess
import tempfile
import urllib.parse
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .browser_mutations import BrowserMutationBinding
from .command import CommandExecutor
from .credentials import CredentialValue, assert_public_payload
from .evidence import AcceptanceEvidence
from .http import GatewaySession
from .redaction import redact_text
from .web_gates import BrowserResult


SIGNATURE_NAMESPACE = "aiops-pilot-acceptance"


def canonical_statement(statement: dict[str, Any]) -> bytes:
    return (json.dumps(statement, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


class PlaywrightBrowser:
    def __init__(self, *, commands: CommandExecutor, source_root: Path) -> None:
        self.commands = commands
        self.source_root = source_root

    def probe(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | CredentialValue | None = None,
        role: str = "authenticated",
    ) -> BrowserResult:
        password_value = _secret_text(password)
        result = _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_browser.mjs",
            payload={
                "base_url": base_url, "username": username,
                "password": password_value, "role": role,
            },
            known_secrets=(password_value,),
        )
        if set(result.screenshots) != {"desktop.png", "mobile.png"}:
            raise RuntimeError("Playwright browser probe did not produce both screenshots")
        return result


class PlaywrightV01Console:
    def __init__(
        self,
        *,
        commands: CommandExecutor,
        source_root: Path,
        evidence: AcceptanceEvidence,
    ) -> None:
        self.commands = commands
        self.source_root = source_root
        self.evidence = evidence

    def provision_v01(
        self,
        *,
        base_url: str,
        admin_username: str,
        admin_password: str | CredentialValue,
        sre_username: str,
        sre_password: str | CredentialValue,
    ) -> BrowserResult:
        admin_value = _secret_text(admin_password)
        sre_value = _secret_text(sre_password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_v01_browser.mjs",
            payload={
                "base_url": base_url,
                "admin_username": admin_username,
                "admin_password": admin_value,
                "sre_username": sre_username,
                "sre_password": sre_value,
            },
            known_secrets=(admin_value, sre_value),
            mutation_binding=BrowserMutationBinding(self.evidence, "V01"),
        )

    def create_v04(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        desired_outcome: str,
        context: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v04", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id, "desired_outcome": desired_outcome,
                "context": context,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V04"),
        )

    def verify_r05(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        dry_run_hash: str,
        target_confirmation: str,
        run_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r05", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": phase_id,
                "revision_id": revision_id,
                "dry_run_hash": dry_run_hash,
                "target_confirmation": target_confirmation,
                "run_id": run_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R05"),
        )

    def execute_v05(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        change_request_id: str,
        target_confirmation: str,
        run_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v05", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "target_confirmation": target_confirmation,
                "approval_reason": f"Approve controlled rollout for run {run_id}",
                "execution_reason": f"Execute controlled rollout for run {run_id}",
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V05"),
        )

    def publish_v07(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        narrative: dict[str, str],
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_report.mjs",
            payload={
                "base_url": base_url,
                "username": username,
                "password": value,
                "incident_id": incident_id,
                "narrative": narrative,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V07"),
        )

    def reinvestigate_v08(
        self, *, base_url: str, username: str, password: str | CredentialValue,
        incident_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands, source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v08_reinvestigate", "base_url": base_url,
                "username": username, "password": value, "incident_id": incident_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V08"),
        )

    def create_v08(
        self, *, base_url: str, username: str, password: str | CredentialValue,
        incident_id: str, run_id: str, desired_outcome: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands, source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v08_create", "base_url": base_url,
                "username": username, "password": value, "incident_id": incident_id,
                "desired_outcome": desired_outcome,
                "context": (
                    f"Second governed recovery for run_id={run_id}; preserve exact Incident scope."
                ),
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V08"),
        )

    def verify_v08_denial(
        self, *, base_url: str, username: str, password: str | CredentialValue,
        incident_id: str, change_request_id: str, phase_id: str,
        revision_id: str, dry_run_hash: str, target_confirmation: str, run_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands, source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v08_denial", "base_url": base_url,
                "username": username, "password": value, "incident_id": incident_id,
                "change_request_id": change_request_id, "phase_id": phase_id,
                "revision_id": revision_id, "dry_run_hash": dry_run_hash,
                "target_confirmation": target_confirmation, "run_id": run_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V08"),
        )

    def execute_v08(
        self, *, base_url: str, username: str, password: str | CredentialValue,
        incident_id: str, change_request_id: str, target_confirmation: str, run_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands, source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "v08_execute", "base_url": base_url,
                "username": username, "password": value, "incident_id": incident_id,
                "change_request_id": change_request_id,
                "target_confirmation": target_confirmation,
                "approval_reason": f"Approve second controlled recovery for run {run_id}",
                "execution_reason": f"Execute second controlled recovery for run {run_id}",
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V08"),
        )

    def publish_v08(
        self, *, base_url: str, username: str, password: str | CredentialValue,
        incident_id: str, narrative: dict[str, str],
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands, source_root=self.source_root,
            script="pilot_acceptance_report.mjs",
            payload={
                "base_url": base_url, "username": username, "password": value,
                "incident_id": incident_id, "narrative": narrative,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "V08"),
        )

    def prepare_r03(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        connector_id: str,
        cluster_id: str,
        operation_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r03_prepare", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "desired_outcome": (
                    "Add one R03 probe annotation to Deployment "
                    "aiops-verification/verification-api"
                ),
                "context": (
                    "Prepare only; do not execute. Patch only top-level annotation "
                    f"aiops.dev/r03-grant-probe={operation_id}. "
                    f"connector_id={connector_id} cluster_id={cluster_id}"
                ),
                "operation_id": operation_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R03"),
        )

    def verify_r03_admin_denial(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        cluster_id: str,
        operation_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r03_admin", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": "none", "cluster_id": cluster_id,
                "operation_id": operation_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R03"),
        )

    def verify_r03_sre_denials(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        prepared: dict[str, object],
        operation_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r03_sre", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id, "prepared": prepared,
                "desired_outcome": "Probe Connector-offline dry-run rejection",
                "context": f"No execution; operation_id={operation_id}",
                "operation_id": operation_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R03"),
        )

    def prepare_r06(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        cluster_id: str,
        operation_id: str,
        approved_value: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r06_prepare", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "desired_outcome": (
                    "Set one R06 stale probe annotation on Deployment "
                    "aiops-verification/verification-api"
                ),
                "context": (
                    "Prepare only; do not execute. Patch only top-level annotation "
                    f"aiops.dev/r06-stale-probe={approved_value}. "
                    f"cluster_id={cluster_id} operation_id={operation_id}"
                ),
                "operation_id": operation_id,
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R06"),
        )

    def approve_r06(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        prepared: dict[str, object],
        operation_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r06_approve", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "change_request_id": prepared["change_request_id"],
                "target_confirmation": prepared["target_confirmation"],
                "approval_reason": f"Approve exact R06 stale probe {operation_id}",
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R06"),
        )

    def start_r06(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        prepared: dict[str, object],
        operation_id: str,
    ) -> BrowserResult:
        value = _secret_text(password)
        return _run_playwright(
            commands=self.commands,
            source_root=self.source_root,
            script="pilot_acceptance_governed_change.mjs",
            payload={
                "action": "r06_start", "base_url": base_url,
                "username": username, "password": value,
                "incident_id": incident_id,
                "change_request_id": prepared["change_request_id"],
                "execution_reason": f"Start exact R06 stale probe {operation_id}",
            },
            known_secrets=(value,),
            mutation_binding=BrowserMutationBinding(self.evidence, "R06"),
        )


def _run_playwright(
    *,
    commands: CommandExecutor,
    source_root: Path,
    script: str,
    payload: dict[str, object],
    known_secrets: tuple[str, ...],
    mutation_binding: BrowserMutationBinding | None = None,
) -> BrowserResult:
    binding_context = mutation_binding if mutation_binding is not None else nullcontext()
    with binding_context as binding, tempfile.TemporaryDirectory(prefix="aiops-acceptance-browser-") as temporary:
        screenshot_dir = Path(temporary) / "screenshots"
        callback = binding.callback if isinstance(binding, BrowserMutationBinding) else None
        redaction_secrets = known_secrets + (
            (callback["token"],) if callback is not None else ()
        )
        stdin = json.dumps(
            {**payload, "screenshot_dir": str(screenshot_dir), "mutation_callback": callback},
            separators=(",", ":"),
        )
        result = commands.run(
            ["node", str(source_root / f"scripts/{script}")],
            cwd=source_root / "apps/aiops_console_web",
            stdin=stdin,
            timeout=180,
        )
        if result.exit_code != 0:
            detail = redact_text(
                result.stderr or result.stdout, known_secrets=redaction_secrets
            )
            raise RuntimeError(f"Playwright browser probe failed: {detail.strip()}")
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Playwright browser probe returned invalid JSON") from exc
        if isinstance(binding, BrowserMutationBinding):
            summary["mutations"] = [
                {
                    "request_id": item.request_id,
                    "method": item.method,
                    "path": item.path,
                    "status": item.status,
                    "response_request_id": item.response_request_id,
                    "identities": item.identities,
                    **({"error_code": item.error_code} if item.error_code else {}),
                }
                for item in binding.facts
            ]
        summary["command"] = _command_summary(result)
        assert_public_payload(summary)
        screenshots = {
            path.name: path.read_bytes() for path in sorted(screenshot_dir.glob("*.png"))
        }
        if not screenshots:
            raise RuntimeError("Playwright browser probe did not produce a screenshot")
        return BrowserResult(summary=summary, screenshots=screenshots)


def _secret_text(value: str | CredentialValue | None) -> str:
    return value.reveal() if isinstance(value, CredentialValue) else value or ""


class OpenSshSigner:
    def sign(self, statement: dict[str, Any], *, key_path: Path) -> dict[str, str]:
        with tempfile.TemporaryDirectory(prefix="aiops-acceptance-sign-") as temporary:
            statement_path = Path(temporary) / "statement.json"
            statement_path.write_bytes(canonical_statement(statement))
            public = self._run(["ssh-keygen", "-y", "-f", str(key_path)]).stdout.strip()
            fingerprint_line = self._run(
                ["ssh-keygen", "-lf", str(key_path), "-E", "sha256"]
            ).stdout.strip()
            fingerprint = next(
                (part for part in fingerprint_line.split() if part.startswith("SHA256:")), ""
            )
            if not public or not fingerprint:
                raise RuntimeError("OpenSSH did not return a public key and SHA256 fingerprint")
            self._run(
                [
                    "ssh-keygen", "-Y", "sign", "-f", str(key_path),
                    "-n", SIGNATURE_NAMESPACE, str(statement_path),
                ]
            )
            signature = Path(f"{statement_path}.sig").read_text(encoding="utf-8")
            return {"signature": signature, "public_key": public, "fingerprint": fingerprint}

    def verify(
        self,
        statement: dict[str, Any],
        *,
        signature: str,
        public_key: str,
        identity: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="aiops-acceptance-verify-") as temporary:
            root = Path(temporary)
            allowed = root / "allowed_signers"
            signature_path = root / "statement.sig"
            allowed.write_text(f"{identity} {public_key}\n", encoding="utf-8")
            signature_path.write_text(signature, encoding="utf-8")
            completed = subprocess.run(
                [
                    "ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", identity,
                    "-n", SIGNATURE_NAMESPACE, "-s", str(signature_path),
                ],
                input=canonical_statement(statement),
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("OpenSSH attestation signature verification failed")

    def fingerprint(self, public_key: str) -> str:
        with tempfile.TemporaryDirectory(prefix="aiops-acceptance-fingerprint-") as temporary:
            path = Path(temporary) / "key.pub"
            path.write_text(public_key.strip() + "\n", encoding="utf-8")
            line = self._run(["ssh-keygen", "-lf", str(path), "-E", "sha256"]).stdout
            value = next((part for part in line.split() if part.startswith("SHA256:")), "")
            if not value:
                raise RuntimeError("OpenSSH did not return a public-key fingerprint")
            return value

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"OpenSSH signing command failed: {detail}")
        return completed


class HttpsProfileProbe:
    def __init__(self, commands: CommandExecutor) -> None:
        self.commands = commands

    def probe(
        self,
        https_url: str,
        *,
        ingress: str,
        http_url: str | None = None,
        require_redirect: bool = False,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(https_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("HTTPS profile URL is invalid")
        host = parsed.hostname
        port = parsed.port or 443
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as connection:
                certificate_der = connection.getpeercert(binary_form=True)
                certificate = connection.getpeercert()
                tls = {
                    "certificate_sha256": hashlib.sha256(certificate_der).hexdigest(),
                    "subject": certificate.get("subject"),
                    "issuer": certificate.get("issuer"),
                    "not_after": certificate.get("notAfter"),
                    "protocol": connection.version(),
                }
        parts = ingress.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("Ingress identity must be namespace/name")
        result = self.commands.run(
            ["kubectl", "-n", parts[0], "get", "ingress", parts[1], "-o", "json"],
            timeout=30,
        )
        if result.exit_code != 0:
            raise RuntimeError("declared HTTPS Ingress could not be read")
        resource = json.loads(result.stdout)
        hosts = sorted(
            str(rule.get("host")) for rule in resource.get("spec", {}).get("rules", [])
        )
        tls_entries = resource.get("spec", {}).get("tls", [])
        if host not in hosts or not any(host in item.get("hosts", []) for item in tls_entries):
            raise ValueError("Ingress rules/TLS do not cover the HTTPS host")
        redirect: dict[str, Any] = {"declared": require_redirect}
        if require_redirect:
            if not http_url:
                raise ValueError("declared redirect policy requires the Ingress HTTP URL")
            response = GatewaySession(http_url, follow_redirects=False).request("GET", "/")
            location = response.headers.get("location")
            target = urllib.parse.urljoin(http_url, location or "")
            if response.status not in {301, 302, 307, 308} or urllib.parse.urlsplit(
                target
            ).scheme != "https":
                raise ValueError("declared Ingress HTTP-to-HTTPS redirect policy failed")
            redirect.update({"status": response.status, "target_origin": _origin(target)})
        return {
            "ingress": {
                "namespace": parts[0],
                "name": parts[1],
                "uid": resource.get("metadata", {}).get("uid"),
                "generation": resource.get("metadata", {}).get("generation"),
                "ingress_class_name": resource.get("spec", {}).get("ingressClassName"),
                "hosts": hosts,
                "tls_secret_names": sorted(
                    str(item.get("secretName")) for item in tls_entries if item.get("secretName")
                ),
            },
            "tls": tls,
            "redirect": redirect,
            "command": _command_summary(result),
        }


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _command_summary(result: Any) -> dict[str, Any]:
    return {
        "command": list(result.command),
        "exit_code": result.exit_code,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_seconds": result.duration_seconds,
    }
