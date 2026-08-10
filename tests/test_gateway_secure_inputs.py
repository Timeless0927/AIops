"""K06 Gateway-owned encrypted Secure Input tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import connector_enrollments as _connector_enrollments  # noqa: F401
from apps.aiops_k8s_gateway.secure_inputs import SecureInputError, SecureInputs
from apps.aiops_k8s_gateway.change_requests import ChangeRequests
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase


def _key(path: Path, value: bytes = b"k" * 32) -> Path:
    path.write_bytes(base64.urlsafe_b64encode(value))
    return path


def _owner(store: GatewayDatabase) -> str:
    users = IdentityAdministration(store).state()["users"]
    if not users:
        IdentityAdministration(store).mutate(
            collection="users", target_id=None,
            payload={"username": "operator", "display_name": "Operator", "password": "strong-password"},
            actor_id="admin", reason="test", action="users_create", request_id="req-user",
        )
        users = IdentityAdministration(store).state()["users"]
    return str(users[0]["id"])


def test_user_and_generated_values_are_encrypted_and_never_projected(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    key_path = _key(tmp_path / "change.key")
    inputs = SecureInputs(store, key_path=key_path, id_factory=lambda: "opaque-1")

    supplied = inputs.create(
        actor_id=_owner(store), key_name="database.password", value="correct horse battery staple",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    replay = inputs.create(
        actor_id=_owner(store), key_name="database.password", value="correct horse battery staple",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-replay",
    )
    generated = SecureInputs(
        store, key_path=key_path, id_factory=lambda: "opaque-2",
        random_bytes=lambda size: b"g" * size,
    ).create(
        actor_id=_owner(store), key_name="api.token", value=None,
        generated_bytes=32, idempotency_key="secure-2", request_id="req-secure-2",
    )

    assert replay == {**supplied, "idempotent": True}
    assert supplied == {
        "id": "opaque-1",
        "key_name": "database.password",
        "placeholder": "{{secure-input:opaque-1}}",
        "sha256": supplied["sha256"],
        "source": "user",
        "created_at": supplied["created_at"],
        "expires_at": supplied["expires_at"],
        "available": True,
        "idempotent": False,
    }
    assert generated["source"] == "generated"
    assert "value" not in generated
    raw_db = (tmp_path / "gateway.db").read_bytes()
    assert b"correct horse battery staple" not in raw_db
    assert b"g" * 32 not in raw_db
    with store.connect() as conn:
        audits = conn.execute(
            "SELECT actor_id, after_json, request_id FROM admin_audit "
            "WHERE action = 'secure_input_create' ORDER BY id",
        ).fetchall()
    assert [row["request_id"] for row in audits] == ["req-secure-1", "req-secure-2"]
    assert json.loads(str(audits[0]["after_json"])) == {
        "key_name": "database.password",
        "sha256": supplied["sha256"],
        "source": "user",
    }
    assert all(row["actor_id"] == _owner(store) for row in audits)


def test_encrypted_refs_require_the_same_current_key_and_never_regenerate(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    key_path = _key(tmp_path / "change.key")
    inputs = SecureInputs(store, key_path=key_path, id_factory=lambda: "opaque-1")
    created = inputs.create(
        actor_id=_owner(store), key_name="token", value="top-secret", generated_bytes=None,
        idempotency_key="secure-1", request_id="req-secure-1",
    )

    with store.connect() as conn:
        refs = inputs.encrypted_refs_for_value_in(
            conn, actor_id=_owner(store),
            value={"token": created["placeholder"]},
        )
    assert [{key: ref[key] for key in ("id", "key_name", "sha256", "placeholder")} for ref in refs] == [{
        "id": "opaque-1", "key_name": "token", "sha256": created["sha256"],
        "placeholder": "{{secure-input:opaque-1}}",
    }]
    assert "top-secret" not in str(refs)

    _key(key_path, b"r" * 32)
    with store.connect() as conn, pytest.raises(SecureInputError) as lost:
        inputs.encrypted_refs_for_value_in(
            conn, actor_id=_owner(store), value={"token": created["placeholder"]},
        )
    assert lost.value.code == "secure_input_unavailable"
    assert inputs.get("opaque-1", actor_id=_owner(store))["available"] is False


def test_generated_value_is_created_once_before_idempotent_replay(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    calls = 0

    def random_bytes(size: int) -> bytes:
        nonlocal calls
        calls += 1
        return bytes([calls]) * size

    inputs = SecureInputs(
        store, key_path=_key(tmp_path / "change.key"),
        id_factory=lambda: "opaque-1", random_bytes=random_bytes,
    )
    created = inputs.create(
        actor_id=_owner(store), key_name="token", value=None, generated_bytes=32,
        idempotency_key="secure-1", request_id="req-secure-1",
    )
    replay = inputs.create(
        actor_id=_owner(store), key_name="token", value=None, generated_bytes=32,
        idempotency_key="secure-1", request_id="req-secure-replay",
    )

    assert replay == {**created, "idempotent": True}
    assert calls == 2  # one value and one AES-GCM nonce


def test_terminal_cleanup_deletes_ciphertext_but_keeps_redacted_facts(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    inputs = SecureInputs(
        store, key_path=_key(tmp_path / "change.key"),
        clock=lambda: 100.0, id_factory=lambda: "opaque-1",
    )
    created = inputs.create(
        actor_id=_owner(store), key_name="token", value="top-secret", generated_bytes=None,
        idempotency_key="secure-1", request_id="req-secure-1",
    )
    with store.connect() as conn:
        ciphertext = bytes(conn.execute(
            "SELECT ciphertext FROM secure_inputs WHERE id = 'opaque-1'",
        ).fetchone()[0])
        inputs.schedule_cleanup_in(conn, ["opaque-1"], delete_after=200.0)
        conn.commit()

    assert inputs.cleanup_expired(now=199.0) == 0
    assert inputs.cleanup_expired(now=200.0) == 1
    projected = inputs.get("opaque-1", actor_id=_owner(store))
    assert projected["sha256"] == created["sha256"]
    assert projected["available"] is False
    with store.connect() as conn:
        row = conn.execute(
            "SELECT ciphertext, nonce, deleted_at FROM secure_inputs WHERE id = 'opaque-1'"
        ).fetchone()
    assert row is not None and row["ciphertext"] is None and row["nonce"] is None
    assert row["deleted_at"] == 200.0
    assert ciphertext not in store.db_path.read_bytes()


def test_shared_input_is_deleted_only_after_every_revision_releases_it(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    inputs = SecureInputs(
        store, key_path=_key(tmp_path / "change.key"),
        clock=lambda: 100.0, id_factory=lambda: "opaque-1",
    )
    created = inputs.create(
        actor_id=_owner(store), key_name="token", value="top-secret", generated_bytes=None,
        idempotency_key="secure-1", request_id="req-secure-1",
    )
    refs = [{"id": created["id"]}]
    with store.connect() as conn:
        inputs.hold_revision_in(conn, "revision-1", refs)
        inputs.hold_revision_in(conn, "revision-2", refs)
        inputs.release_revision_in(
            conn, "revision-1", released_at=150.0, delete_after=200.0,
        )
        conn.commit()

    assert inputs.cleanup_expired(now=200.0) == 0
    assert inputs.get("opaque-1", actor_id=_owner(store))["available"] is True

    with store.connect() as conn:
        inputs.release_revision_in(
            conn, "revision-2", released_at=250.0, delete_after=300.0,
        )
        conn.commit()
    assert inputs.cleanup_expired(now=299.0) == 0
    assert inputs.cleanup_expired(now=300.0) == 1


def test_model_context_contains_only_the_opaque_placeholder(tmp_path: Path) -> None:
    store = GatewayDatabase(tmp_path / "gateway.db")
    inputs = SecureInputs(
        store, key_path=_key(tmp_path / "change.key"), id_factory=lambda: "opaque-1",
    )
    owner = _owner(store)
    secure = inputs.create(
        actor_id=owner, key_name="api.token", value="must-never-reach-model",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)",
        )
    seen: list[dict[str, object]] = []
    ChangeRequests(store).submit(
        incident_id="incident-1", facts={"resource": {"cluster_id": "cluster-prod"}},
        actor_id=owner, desired_outcome="configure checkout integration",
        context=str(secure["placeholder"]), idempotency_key="change-1", request_id="req-change",
        planner=lambda payload: seen.append(payload) or {
            "status": "needs_input", "question": "Which target should use the supplied value?",
        },
    )

    assert len(seen) == 1
    assert secure["placeholder"] in json.dumps(seen[0])
    assert "must-never-reach-model" not in json.dumps(seen[0])
