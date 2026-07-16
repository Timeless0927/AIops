"""Run-scoped Acceptance Runner credential sources and tmpfs storage."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .command import CommandExecutor


_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_MAX_SECRET_BYTES = 64 * 1024
_FORBIDDEN_PUBLIC_FIELDS = (
    "apikey", "authorization", "cookie", "password", "privatekey", "secret", "session", "token",
)


class CredentialError(ValueError):
    pass


class CredentialValue:
    """Plaintext available only through an explicit in-memory reveal."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value or len(value.encode()) > _MAX_SECRET_BYTES:
            raise CredentialError("credential must be non-empty and bounded")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "CredentialValue(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class BootstrapCredential:
    namespace: str
    secret_name: str
    secret_uid: str
    resource_version: str
    key: str
    value: CredentialValue


class KubernetesBootstrapCredentialSource:
    """Reread the exact bootstrap password without copying it to the run store."""

    def __init__(self, commands: CommandExecutor) -> None:
        self._commands = commands

    def read(
        self,
        *,
        kube_context: str,
        namespace: str = "aiops-system",
        secret_name: str = "aiops-runtime-secret",
        key: str = "AIOPS_BOOTSTRAP_ADMIN_PASSWORD",
    ) -> BootstrapCredential:
        result = self._commands.run(
            [
                "kubectl", "--context", kube_context, "-n", namespace,
                "get", "secret", secret_name, "-o", "json",
            ],
            timeout=30,
        )
        if result.exit_code != 0:
            raise CredentialError("bootstrap credential Secret is unavailable")
        try:
            payload = json.loads(result.stdout)
            metadata = payload["metadata"]
            encoded = payload["data"][key]
            raw = base64.b64decode(encoded, validate=True)
            value = raw.decode("utf-8")
        except (KeyError, TypeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise CredentialError("bootstrap credential Secret is invalid") from exc
        if metadata.get("name") != secret_name or metadata.get("namespace") != namespace:
            raise CredentialError("bootstrap credential Secret identity mismatch")
        uid = metadata.get("uid")
        revision = metadata.get("resourceVersion")
        if not isinstance(uid, str) or not uid or not isinstance(revision, str) or not revision:
            raise CredentialError("bootstrap credential Secret lacks durable identity")
        return BootstrapCredential(
            namespace, secret_name, uid, revision, key, CredentialValue(value),
        )


class RunCredentialStore:
    """Store acceptance-created and imported credentials in one run tmpfs directory."""

    def __init__(
        self,
        root: Path,
        *,
        workspace: Path,
        evidence_root: Path,
        mountinfo: Callable[[], str] | None = None,
    ) -> None:
        self._requested_root = root.expanduser().absolute()
        self.root = self._requested_root.resolve()
        self._workspace = workspace.resolve()
        self._evidence_root = evidence_root.resolve()
        self._mountinfo = mountinfo or (lambda: Path("/proc/self/mountinfo").read_text())

    def create(self) -> "RunCredentialStore":
        self._validate_location()
        if self._requested_root.is_symlink():
            raise CredentialError("credential store must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.root, 0o700)
        return self

    def open(self) -> "RunCredentialStore":
        self._validate_location()
        self._validate_directory()
        return self

    def generate_user_password(self, name: str) -> CredentialValue:
        path = self._path(name)
        if not path.exists():
            self._write(path, secrets.token_urlsafe(32).encode())
        return self.read(name)

    def import_secret(self, name: str, source: Path) -> CredentialValue:
        data = self._read_external_source(source)
        try:
            value = CredentialValue(data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CredentialError("credential input must be UTF-8 text") from exc
        self._write(self._path(name), data)
        return value

    def read(self, name: str) -> CredentialValue:
        self._validate_directory()
        path = self._path(name)
        self._validate_file(path)
        data = path.read_bytes()
        if not data or len(data) > _MAX_SECRET_BYTES:
            raise CredentialError("stored credential must be non-empty and bounded")
        try:
            return CredentialValue(data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CredentialError("stored credential must be UTF-8 text") from exc

    def require(self, *names: str) -> None:
        for name in names:
            self.read(name)

    def cleanup(self) -> None:
        if self._requested_root.is_symlink() or self.root.is_symlink():
            raise CredentialError("credential store must not be a symlink")
        if self.root.exists():
            shutil.rmtree(self.root)

    def cleanup_if_terminal(self, ledger: object) -> bool:
        failed = getattr(ledger, "failed_gate", None) is not None
        ledger_root = Path(getattr(ledger, "root"))
        sealed = (ledger_root / "SHA256SUMS").is_file()
        if failed or sealed:
            self.cleanup()
            return True
        return False

    def _validate_location(self) -> None:
        if self.root.is_relative_to(self._workspace) or self.root.is_relative_to(self._evidence_root):
            raise CredentialError("credential store must be outside workspace and evidence")
        if not _is_tmpfs(self.root.parent, self._mountinfo()):
            raise CredentialError("credential store parent must be tmpfs")

    def _validate_directory(self) -> None:
        if self._requested_root.is_symlink() or self.root.is_symlink() or not self.root.is_dir():
            raise CredentialError("credential store is unavailable")
        if not _is_tmpfs(self.root, self._mountinfo()):
            raise CredentialError("credential store must remain on tmpfs")
        if stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise CredentialError("credential store directory mode must be 0700")

    def _read_external_source(self, source: Path) -> bytes:
        requested = source.expanduser().absolute()
        if requested.is_symlink():
            raise CredentialError("credential input must be a regular file")
        resolved = requested.resolve()
        if resolved.is_relative_to(self._workspace) or resolved.is_relative_to(self._evidence_root):
            raise CredentialError("credential input must be outside workspace and evidence")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(requested, flags)
        except OSError as exc:
            raise CredentialError("credential input must be a regular file") from exc
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise CredentialError("credential input must be a regular file")
            if stat.S_IMODE(status.st_mode) != 0o600:
                raise CredentialError("credential input mode must be 0600")
            if status.st_size <= 0 or status.st_size > _MAX_SECRET_BYTES:
                raise CredentialError("credential input must be non-empty and bounded")
            return stream.read(_MAX_SECRET_BYTES + 1)

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise CredentialError("credential name contains unsupported characters")
        return self.root / name

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        if path.exists():
            raise CredentialError("credential already exists")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise CredentialError("credential is unavailable; password reset is forbidden")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise CredentialError("credential file mode must be 0600")


def assert_public_payload(value: object) -> None:
    """Reject secret-bearing field names without inspecting secret plaintext."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _FORBIDDEN_PUBLIC_FIELDS):
                raise CredentialError(f"public payload contains forbidden field {key!r}")
            assert_public_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_public_payload(item)


def _is_tmpfs(path: Path, mountinfo: str) -> bool:
    resolved = path.resolve()
    matches: list[tuple[int, str]] = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        filesystem = after.split()[0] if after.split() else ""
        if len(fields) < 5 or filesystem != "tmpfs":
            continue
        mount = Path(fields[4].replace("\\040", " ")).resolve()
        if resolved == mount or resolved.is_relative_to(mount):
            matches.append((len(mount.parts), filesystem))
    return bool(matches)
