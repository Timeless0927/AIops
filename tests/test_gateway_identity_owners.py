from __future__ import annotations

from pathlib import Path

import sqlite3

from aiops.domain.identity import Actor
from apps.aiops_k8s_gateway.gateway_audit import GatewayAudit
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.gateway_sessions import GatewaySessions
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration


def test_session_owner_uses_injected_clock_token_and_active_actor_lookup(tmp_path: Path) -> None:
    now = [100.0]
    actor = Actor(actor_id="user-1", username="sre", display_name="SRE")
    active_actor = [actor]
    sessions = GatewaySessions(
        GatewayDatabase(tmp_path / "gateway.db"),
        clock=lambda: now[0],
        token_factory=lambda: "fixed-token",
        active_actor_lookup=lambda _username: active_actor[0],
    )

    issued = sessions.issue(actor)
    assert (issued.token, issued.created_at, issued.expires_at) == ("fixed-token", 100.0, 28_900.0)
    assert sessions.lookup("fixed-token") == issued

    active_actor[0] = None
    assert sessions.lookup("fixed-token") is None


def test_identity_mutation_audits_and_revokes_sessions_in_one_owner_transaction(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    now = [200.0]
    audit = GatewayAudit(database, clock=lambda: now[0])
    sessions = GatewaySessions(
        database,
        clock=lambda: 200.0,
        token_factory=lambda: "user-token",
        active_actor_lookup=lambda _username: Actor(actor_id="user-1", username="sre", display_name="SRE"),
    )
    identities = IdentityAdministration(
        database,
        clock=lambda: 200.0,
        id_factory=lambda prefix: {"usr": "user-1"}[prefix],
        audit_insert=audit.insert_in,
        revoke_actor_in=sessions.revoke_actor_in,
    )
    _, user = identities.mutate(
        collection="users",
        target_id=None,
        payload={"username": "sre", "display_name": "SRE", "password": "old-pass"},
        actor_id="admin-1",
        reason="create user",
        action="users_create",
        request_id="request-1",
    )
    sessions.issue(Actor(actor_id="user-1", username="sre", display_name="SRE"))

    now[0] = 201.0
    identities.mutate(
        collection="users",
        target_id=str(user["id"]),
        payload={"password": "new-pass"},
        actor_id="admin-1",
        reason="rotate password",
        action="users_update",
        request_id="request-2",
    )

    assert sessions.lookup("user-token") is None
    assert [row["request_id"] for row in audit.recent()[:2]] == ["request-2", "request-1"]


def test_identity_mutation_rolls_back_when_transaction_local_audit_fails(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    audit = GatewayAudit(database, clock=lambda: 300.0)
    identities = IdentityAdministration(
        database,
        clock=lambda: 300.0,
        id_factory=lambda _prefix: "team-1",
        audit_insert=audit.insert_in,
        revoke_actor_in=GatewaySessions.revoke_actor_in,
    )
    with database.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_admin_audit BEFORE INSERT ON admin_audit
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
            """
        )

    try:
        identities.mutate(
            collection="teams",
            target_id=None,
            payload={"name": "Must Roll Back"},
            actor_id="admin-1",
            reason="verify atomic audit",
            action="teams_create",
            request_id="request-1",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("audit failure must abort the identity mutation")

    assert identities.state()["teams"] == []
