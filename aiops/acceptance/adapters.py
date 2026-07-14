"""Concrete browser and OpenSSH adapters for the acceptance CLI."""

from __future__ import annotations

import json
import hashlib
import socket
import ssl
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from .command import CommandExecutor
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
        password: str | None = None,
    ) -> BrowserResult:
        with tempfile.TemporaryDirectory(prefix="aiops-acceptance-browser-") as temporary:
            screenshot_dir = Path(temporary) / "screenshots"
            payload = json.dumps(
                {
                    "base_url": base_url,
                    "username": username,
                    "password": password,
                    "screenshot_dir": str(screenshot_dir),
                },
                separators=(",", ":"),
            )
            result = self.commands.run(
                ["node", str(self.source_root / "scripts/pilot_acceptance_browser.mjs")],
                cwd=self.source_root / "apps/aiops_console_web",
                stdin=payload,
                timeout=90,
            )
            if result.exit_code != 0:
                detail = redact_text(result.stderr or result.stdout, known_secrets=[password or ""])
                raise RuntimeError(f"Playwright browser probe failed: {detail.strip()}")
            try:
                summary = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Playwright browser probe returned invalid JSON") from exc
            summary["command"] = _command_summary(result)
            screenshots = {
                path.name: path.read_bytes() for path in sorted(screenshot_dir.glob("*.png"))
            }
            if set(screenshots) != {"desktop.png", "mobile.png"}:
                raise RuntimeError("Playwright browser probe did not produce both screenshots")
            return BrowserResult(summary=summary, screenshots=screenshots)


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
