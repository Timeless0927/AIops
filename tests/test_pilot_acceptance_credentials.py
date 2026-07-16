from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.credentials import (
    CredentialError,
    KubernetesBootstrapCredentialSource,
    RunCredentialStore,
    assert_public_payload,
)


class BootstrapCommands:
    command = None

    def run(self, command, **_kwargs):
        self.command = tuple(command)
        return CommandResult(
            tuple(command),
            0,
            json.dumps({
                "metadata": {
                    "name": "aiops-runtime-secret",
                    "namespace": "aiops-system",
                    "uid": "secret-uid-1",
                    "resourceVersion": "42",
                },
                "data": {
                    "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": base64.b64encode(
                        b"dynamic-admin-password"
                    ).decode(),
                },
            }),
            "",
            0.1,
        )


def _store(tmp_path: Path) -> RunCredentialStore:
    root = Path("/dev/shm") / f"aiops-acceptance-test-{uuid.uuid4().hex}"
    return RunCredentialStore(
        root,
        workspace=tmp_path / "workspace",
        evidence_root=tmp_path / "evidence",
    )


def test_bootstrap_password_is_reread_from_exact_secret_without_plaintext_argv() -> None:
    commands = BootstrapCommands()
    credential = KubernetesBootstrapCredentialSource(commands).read(
        kube_context="pilot-context"
    )

    assert credential.value.reveal() == "dynamic-admin-password"
    assert credential.secret_uid == "secret-uid-1"
    assert credential.resource_version == "42"
    assert commands.command == (
        "kubectl", "--context", "pilot-context", "-n", "aiops-system",
        "get", "secret", "aiops-runtime-secret", "-o", "json",
    )
    assert "dynamic-admin-password" not in " ".join(commands.command)
    assert "dynamic-admin-password" not in repr(credential.value)


def test_run_store_generates_once_imports_mode_0600_and_fails_on_loss(tmp_path: Path) -> None:
    store = _store(tmp_path).create()
    source = tmp_path / "notification-input"
    source.write_text("provider-secret", encoding="utf-8")
    os.chmod(source, 0o600)
    try:
        first = store.generate_user_password("sre.password").reveal()
        second = store.generate_user_password("sre.password").reveal()
        imported = store.import_secret("notification.secret", source)

        assert first == second and len(first) >= 32
        assert imported.reveal() == "provider-secret"
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((store.root / "sre.password").stat().st_mode) == 0o600
        assert stat.S_IMODE((store.root / "notification.secret").stat().st_mode) == 0o600

        (store.root / "sre.password").unlink()
        with pytest.raises(CredentialError, match="password reset is forbidden"):
            store.read("sre.password")
    finally:
        store.cleanup()


def test_run_store_rejects_ordinary_disk_bad_modes_and_terminal_runs(tmp_path: Path) -> None:
    ordinary = RunCredentialStore(
        tmp_path / "credentials",
        workspace=tmp_path / "workspace",
        evidence_root=tmp_path / "evidence",
    )
    with pytest.raises(CredentialError, match="tmpfs"):
        ordinary.create()

    store = _store(tmp_path).create()
    source = tmp_path / "model-input"
    source.write_text("model-secret", encoding="utf-8")
    os.chmod(source, 0o644)
    try:
        with pytest.raises(CredentialError, match="0600"):
            store.import_secret("model.secret", source)

        class FailedLedger:
            failed_gate = "S03"
            root = tmp_path / "evidence"

        assert store.cleanup_if_terminal(FailedLedger()) is True
        assert not store.root.exists()
    finally:
        store.cleanup()

    sealed = _store(tmp_path).create()
    sealed_ledger_root = tmp_path / "sealed-evidence"
    sealed_ledger_root.mkdir()
    (sealed_ledger_root / "SHA256SUMS").write_text("sealed\n", encoding="utf-8")

    class SealedLedger:
        failed_gate = None
        root = sealed_ledger_root

    assert sealed.cleanup_if_terminal(SealedLedger()) is True
    assert not sealed.root.exists()

    store = _store(tmp_path).create()
    real_source = tmp_path / "real-input"
    real_source.write_text("secret", encoding="utf-8")
    os.chmod(real_source, 0o600)
    linked_source = tmp_path / "linked-input"
    linked_source.symlink_to(real_source)
    try:
        with pytest.raises(CredentialError, match="regular file"):
            store.import_secret("linked.secret", linked_source)
    finally:
        store.cleanup()


def test_public_payload_scan_uses_structure_not_secret_plaintext() -> None:
    assert_public_payload({"request_id": "req-1", "object": {"id": "user-1", "revision": 2}})
    with pytest.raises(CredentialError, match="forbidden field"):
        assert_public_payload({"object": {"session_token": "not-inspected"}})
    with pytest.raises(CredentialError, match="forbidden field"):
        assert_public_payload({"apiKey": "not-inspected"})
