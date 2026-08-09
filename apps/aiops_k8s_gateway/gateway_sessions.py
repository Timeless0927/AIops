"""Gateway-owned browser and bearer sessions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from aiops.domain.identity import Actor, AuthSession, IdentityError, ROLE_VIEWER

from .gateway_db import GatewayDatabase, token_hash


class GatewaySessions:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float],
        token_factory: Callable[[], str],
        active_actor_lookup: Callable[[str], Actor | None],
        ttl_seconds: int = 8 * 60 * 60,
    ) -> None:
        self._database = database
        self._clock = clock
        self._token_factory = token_factory
        self._active_actor_lookup = active_actor_lookup
        self.ttl_seconds = ttl_seconds

    def issue(self, actor: Actor) -> AuthSession:
        now = self._clock()
        token = self._token_factory()
        session = AuthSession(token=token, actor=actor, created_at=now, expires_at=now + self.ttl_seconds)
        with self._database.connect() as conn:
            conn.execute(
                "INSERT INTO session_actors (actor_id, actor_json) VALUES (?, ?) "
                "ON CONFLICT(actor_id) DO UPDATE SET actor_json = excluded.actor_json",
                (actor.actor_id, json.dumps(actor.to_dict(), separators=(",", ":"))),
            )
            conn.execute(
                "INSERT INTO sessions (token_hash, actor_id, created_at, expires_at, fresh_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash(token), actor.actor_id, session.created_at, session.expires_at, now),
            )
        return session

    def lookup(self, token: str) -> AuthSession | None:
        if not token:
            return None
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            row = conn.execute(
                """
                SELECT s.created_at, s.expires_at, a.actor_json
                FROM sessions s
                JOIN session_actors a ON a.actor_id = s.actor_id
                WHERE s.token_hash = ?
                """,
                (token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        snapshot = Actor.from_mapping(json.loads(str(row["actor_json"])))
        try:
            actor = self._active_actor_lookup(snapshot.username)
        except IdentityError:
            actor = None
        if actor is None:
            self.revoke(token)
            return None
        if self.actor_view(actor)["is_platform_administrator"]:
            actor = Actor(
                actor_id=actor.actor_id,
                username=actor.username,
                display_name=actor.display_name,
                email=actor.email,
                roles=(ROLE_VIEWER,),
                scope=actor.scope,
                groups=actor.groups,
                department=actor.department,
                auth_source=actor.auth_source,
            )
        return AuthSession(
            token=token,
            actor=actor,
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
        )

    def is_fresh(self, token: str, *, max_age_seconds: int = 5 * 60) -> bool:
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT fresh_at FROM sessions WHERE token_hash = ?",
                (token_hash(token),),
            ).fetchone()
        return row is not None and self._clock() - float(row["fresh_at"] or 0) <= max_age_seconds

    def mark_fresh(self, token: str) -> None:
        with self._database.connect() as conn:
            conn.execute(
                "UPDATE sessions SET fresh_at = ? WHERE token_hash = ?",
                (self._clock(), token_hash(token)),
            )

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._database.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    @staticmethod
    def revoke_actor_in(conn: sqlite3.Connection, actor_id: str) -> None:
        conn.execute("DELETE FROM sessions WHERE actor_id = ?", (actor_id,))

    def actor_view(self, actor: Actor) -> dict[str, object]:
        with self._database.connect() as conn:
            bindings = conn.execute(
                """
                SELECT rb.role
                FROM role_bindings rb
                WHERE rb.user_id = ? AND rb.active = 1 AND (
                    rb.role = 'platform_administrator'
                    OR EXISTS (
                        SELECT 1
                        FROM team_memberships tm
                        JOIN teams t ON t.id = tm.team_id
                        WHERE tm.user_id = rb.user_id AND tm.team_id = rb.scope_id
                          AND tm.active = 1 AND t.active = 1
                    )
                )
                ORDER BY rb.role
                """,
                (actor.actor_id,),
            ).fetchall()
        roles = [str(row["role"]) for row in bindings]
        capabilities = {"view_incident"} if "sre" in roles or "platform_administrator" in roles else set()
        if capabilities:
            capabilities.add("manage_investigation")
        if "platform_administrator" in roles:
            capabilities.add("manage_identity")
        return {
            "id": actor.actor_id,
            "username": actor.username,
            "display_name": actor.display_name,
            "roles": roles,
            "capabilities": sorted(capabilities),
            "is_platform_administrator": "platform_administrator" in roles,
        }
