"""Gateway-owned V1 SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from aiops.domain.identity import Actor, AuthSession, IdentityConfig, IdentityError, ROLE_VIEWER, SQLiteIdentityStore, hash_password

from .gateway_db import insert_admin_audit


_MIGRATIONS = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS session_actors (
            actor_id TEXT PRIMARY KEY,
            actor_json TEXT NOT NULL CHECK (json_valid(actor_json))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
            actor_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL CHECK (expires_at > created_at),
            FOREIGN KEY (actor_id) REFERENCES session_actors(actor_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS sessions_by_expiry ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK (length(title) > 0),
            severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
            status TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL CHECK (updated_at >= created_at)
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE sessions ADD COLUMN fresh_at REAL;
        UPDATE sessions SET fresh_at = created_at WHERE fresh_at IS NULL;

        CREATE TABLE teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE CHECK (length(name) > 0),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE team_memberships (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (user_id, team_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        );

        CREATE TABLE role_bindings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('platform_administrator', 'sre')),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('platform', 'team')),
            scope_id TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (
                (role = 'platform_administrator' AND scope_type = 'platform' AND scope_id IS NULL)
                OR (role = 'sre' AND scope_type = 'team' AND scope_id IS NOT NULL)
            ),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (scope_id) REFERENCES teams(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX role_binding_identity
            ON role_bindings(user_id, role, scope_type, IFNULL(scope_id, ''));

        CREATE TABLE admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT,
            target_type TEXT NOT NULL,
            target_id TEXT,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            result TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX admin_audit_recent ON admin_audit(created_at DESC);
        """,
    ),
    (
        3,
        """
        CREATE TABLE connector_enrollments (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL UNIQUE CHECK (length(connector_id) > 0),
            cluster_id TEXT NOT NULL UNIQUE CHECK (length(cluster_id) > 0),
            credential_hash TEXT NOT NULL UNIQUE CHECK (length(credential_hash) = 64),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE clusters (
            cluster_id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            environment TEXT NOT NULL DEFAULT 'prod' CHECK (environment IN ('prod', 'staging', 'dev', 'test')),
            governance_notes TEXT NOT NULL DEFAULT '',
            mutation_enabled INTEGER NOT NULL DEFAULT 0 CHECK (mutation_enabled IN (0, 1)),
            runtime_status TEXT NOT NULL DEFAULT 'online' CHECK (runtime_status IN ('online', 'offline', 'degraded')),
            failure_summary TEXT NOT NULL DEFAULT '',
            last_heartbeat REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id)
        );
        """,
    ),
)
_MIGRATION_LOCK = threading.Lock()


class GatewayV1Store:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        ttl_seconds: int = 8 * 60 * 60,
        clock: Callable[[], float] = time.time,
        credential_factory: Callable[[], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._configured_path = Path(db_path).expanduser() if db_path else None
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._credential_factory = credential_factory or (lambda: secrets.token_urlsafe(32))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    @property
    def db_path(self) -> Path:
        if self._configured_path is not None:
            return self._configured_path
        return Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser() / "gateway.db"

    def issue(self, actor: Actor) -> AuthSession:
        now = time.time()
        token = secrets.token_urlsafe(32)
        session = AuthSession(token=token, actor=actor, created_at=now, expires_at=now + self.ttl_seconds)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_actors (actor_id, actor_json) VALUES (?, ?) "
                "ON CONFLICT(actor_id) DO UPDATE SET actor_json = excluded.actor_json",
                (actor.actor_id, json.dumps(actor.to_dict(), separators=(",", ":"))),
            )
            conn.execute(
                "INSERT INTO sessions (token_hash, actor_id, created_at, expires_at, fresh_at) VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), actor.actor_id, session.created_at, session.expires_at, now),
            )
        return session

    def get(self, token: str) -> AuthSession | None:
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            row = conn.execute(
                """
                SELECT s.created_at, s.expires_at, a.actor_json
                FROM sessions s
                JOIN session_actors a ON a.actor_id = s.actor_id
                WHERE s.token_hash = ?
                """,
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        snapshot = Actor.from_mapping(json.loads(str(row["actor_json"])))
        identity_store = SQLiteIdentityStore(IdentityConfig.load().store_path)
        try:
            actor = identity_store.get_actor(snapshot.username)
        except IdentityError:
            actor = None
        finally:
            identity_store.close()
        if actor is None:
            self.revoke(token)
            return None
        if self.is_platform_administrator(actor.actor_id):
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fresh_at FROM sessions WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
        return row is not None and time.time() - float(row["fresh_at"] or 0) <= max_age_seconds

    def mark_fresh(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET fresh_at = ? WHERE token_hash = ?",
                (time.time(), _token_hash(token)),
            )

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def revoke_user(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE actor_id = ?", (user_id,))

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM session_actors")

    def list_incidents(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, severity, status, created_at, updated_at FROM incidents ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_platform_administrator(self, user_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            if not _table_exists(conn, "users") or conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO role_bindings (
                    id, user_id, role, scope_type, scope_id, active, created_at, updated_at
                ) VALUES (?, ?, 'platform_administrator', 'platform', NULL, 1, ?, ?)
                """,
                (f"rb-{uuid.uuid4().hex}", user_id, now, now),
            )

    def is_platform_administrator(self, user_id: str) -> bool:
        with self._connect() as conn:
            if not _table_exists(conn, "users"):
                return False
            row = conn.execute(
                """
                SELECT 1
                FROM role_bindings rb
                JOIN users u ON u.id = rb.user_id
                WHERE rb.user_id = ? AND rb.role = 'platform_administrator'
                  AND rb.active = 1 AND u.disabled = 0
                """,
                (user_id,),
            ).fetchone()
        return row is not None

    def actor_view(self, actor: Actor) -> dict[str, object]:
        with self._connect() as conn:
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

    def admin_state(self) -> dict[str, list[dict[str, object]]]:
        return {
            "users": self.list_users(),
            "teams": self.list_teams(),
            "team_memberships": self.list_team_memberships(),
            "role_bindings": self.list_role_bindings(),
        }

    def list_users(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, display_name, email, disabled, updated_at
                FROM users WHERE auth_source = 'local' ORDER BY username
                """
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "username": str(row["username"]),
                "display_name": str(row["display_name"]),
                "email": row["email"],
                "active": not bool(row["disabled"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def user(self, user_id: str) -> dict[str, object] | None:
        return next((user for user in self.list_users() if user["id"] == user_id), None)

    def team(self, team_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            raise IdentityError("not_found", "team not found")
        return _active_record(row)

    def list_teams(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
        return [_active_record(row) for row in rows]

    def team_membership(self, membership_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM team_memberships WHERE id = ?", (membership_id,)).fetchone()
        if row is None:
            raise IdentityError("not_found", "team membership not found")
        return _active_record(row)

    def list_team_memberships(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM team_memberships ORDER BY created_at").fetchall()
        return [_active_record(row) for row in rows]

    def role_binding(self, binding_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM role_bindings WHERE id = ?", (binding_id,)).fetchone()
        if row is None:
            raise IdentityError("not_found", "role binding not found")
        return _active_record(row)

    def list_role_bindings(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM role_bindings ORDER BY created_at").fetchall()
        return [_active_record(row) for row in rows]

    def mutate_admin(
        self,
        *,
        collection: str,
        target_id: str | None,
        payload: dict[str, Any],
        actor_id: str,
        reason: str,
        action: str,
        request_id: str,
    ) -> tuple[str, dict[str, object]]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mutation_complete = False
            try:
                response_key, audit_target, before, after = self._mutate_admin(conn, collection, target_id, payload)
                mutation_complete = True
                insert_admin_audit(
                    conn,
                    actor_id=actor_id,
                    target_type=collection,
                    target_id=audit_target,
                    action=action,
                    reason=reason,
                    before=before,
                    after=after,
                    result="success",
                    request_id=request_id,
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if mutation_complete:
                    raise
                code = {
                    "users": "user_exists",
                    "teams": "team_exists",
                    "team-memberships": "membership_exists",
                    "role-bindings": "role_binding_exists",
                }.get(collection, "invalid_request")
                raise IdentityError(code, str(exc)) from exc
            except BaseException:
                conn.rollback()
                raise
        return response_key, after

    def _mutate_admin(
        self,
        conn: sqlite3.Connection,
        collection: str,
        target_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, object] | None, dict[str, object]]:
        now = time.time()
        if collection == "users":
            if target_id is None:
                username = str(payload.get("username") or "").strip()
                password = str(payload.get("password") or "")
                if not username or not password:
                    raise IdentityError("invalid_user", "username and password are required")
                if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    raise IdentityError("user_exists", "user already exists")
                display_name = str(payload.get("display_name") or "").strip()
                if not display_name:
                    raise IdentityError("invalid_user", "display_name is required")
                target_id = f"usr-{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, email, disabled, created_at,
                        updated_at, password, auth_source
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'local')
                    """,
                    (
                        target_id,
                        username,
                        display_name,
                        payload.get("email"),
                        now,
                        now,
                        hash_password(password),
                    ),
                )
                conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, 'viewer')", (target_id,))
                before = None
            else:
                before = _user_record(conn, target_id)
                display_name = str(payload.get("display_name", before["display_name"])).strip()
                if not display_name:
                    raise IdentityError("invalid_user", "display_name is required")
                active = bool(payload.get("active", before["active"]))
                if not active and _is_platform_admin(conn, target_id) and _active_admin_count(conn) == 1:
                    raise IdentityError("last_admin", "cannot disable the last active Platform Administrator")
                conn.execute(
                    "UPDATE users SET display_name = ?, email = ?, disabled = ?, updated_at = ? WHERE id = ?",
                    (display_name, payload.get("email", before["email"]), 0 if active else 1, now, target_id),
                )
                if payload.get("password"):
                    conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(str(payload["password"])), target_id))
                conn.execute("DELETE FROM sessions WHERE actor_id = ?", (target_id,))
            return "user", target_id, before, _user_record(conn, target_id)

        if collection == "teams":
            if target_id is None:
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise IdentityError("invalid_team", "team name is required")
                target_id = f"team-{uuid.uuid4().hex}"
                before = None
                conn.execute(
                    "INSERT INTO teams (id, name, description, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (target_id, name, str(payload.get("description") or "").strip(), now, now),
                )
            else:
                before = _row_record(conn, "teams", target_id, "team")
                name = str(payload.get("name", before["name"])).strip()
                if not name:
                    raise IdentityError("invalid_team", "team name is required")
                conn.execute(
                    "UPDATE teams SET name = ?, description = ?, active = ?, updated_at = ? WHERE id = ?",
                    (name, payload.get("description", before["description"]), int(bool(payload.get("active", before["active"]))), now, target_id),
                )
                conn.execute(
                    "DELETE FROM sessions WHERE actor_id IN (SELECT user_id FROM team_memberships WHERE team_id = ?)",
                    (target_id,),
                )
            return "team", target_id, before, _row_record(conn, "teams", target_id, "team")

        if collection == "team-memberships":
            if target_id is None:
                user_id = str(payload.get("user_id") or "")
                team_id = str(payload.get("team_id") or "")
                _user_record(conn, user_id)
                _row_record(conn, "teams", team_id, "team")
                target_id = f"tm-{uuid.uuid4().hex}"
                before = None
                conn.execute(
                    "INSERT INTO team_memberships (id, user_id, team_id, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (target_id, user_id, team_id, now, now),
                )
            else:
                before = _row_record(conn, "team_memberships", target_id, "team membership")
                conn.execute(
                    "UPDATE team_memberships SET active = ?, updated_at = ? WHERE id = ?",
                    (int(bool(payload.get("active", before["active"]))), now, target_id),
                )
                conn.execute("DELETE FROM sessions WHERE actor_id = ?", (before["user_id"],))
            return "team_membership", target_id, before, _row_record(conn, "team_memberships", target_id, "team membership")

        if collection == "role-bindings":
            if target_id is None:
                user_id = str(payload.get("user_id") or "")
                role = str(payload.get("role") or "")
                scope_type = str(payload.get("scope_type") or "")
                scope_id = str(payload.get("scope_id") or "").strip() or None
                _user_record(conn, user_id)
                if role == "platform_administrator" and scope_type == "platform" and scope_id is None:
                    pass
                elif role == "sre" and scope_type == "team" and scope_id:
                    _row_record(conn, "teams", scope_id, "team")
                    if not conn.execute(
                        "SELECT 1 FROM team_memberships WHERE user_id = ? AND team_id = ? AND active = 1",
                        (user_id, scope_id),
                    ).fetchone():
                        raise IdentityError("membership_required", "an active Team Membership is required")
                else:
                    raise IdentityError("invalid_role_binding", "role and scope are invalid")
                target_id = f"rb-{uuid.uuid4().hex}"
                before = None
                conn.execute(
                    """
                    INSERT INTO role_bindings (
                        id, user_id, role, scope_type, scope_id, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (target_id, user_id, role, scope_type, scope_id, now, now),
                )
            else:
                before = _row_record(conn, "role_bindings", target_id, "role binding")
                active = bool(payload.get("active", before["active"]))
                if before["role"] == "platform_administrator" and before["active"] and not active and _active_admin_count(conn) == 1:
                    raise IdentityError("last_admin", "cannot remove the last active Platform Administrator")
                conn.execute("UPDATE role_bindings SET active = ?, updated_at = ? WHERE id = ?", (int(active), now, target_id))
                conn.execute("DELETE FROM sessions WHERE actor_id = ?", (before["user_id"],))
            return "role_binding", target_id, before, _row_record(conn, "role_bindings", target_id, "role binding")

        raise IdentityError("not_found", "administration resource not found")

    def record_admin_audit(
        self,
        *,
        actor_id: str | None,
        target_type: str,
        target_id: str | None,
        action: str,
        reason: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
        result: str,
        request_id: str,
    ) -> None:
        with self._connect() as conn:
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type=target_type,
                target_id=target_id,
                action=action,
                reason=reason,
                before=before,
                after=after,
                result=result,
                request_id=request_id,
            )

    def list_admin_audit(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT 100").fetchall()
        return [
            {
                **dict(row),
                "before": json.loads(str(row["before_json"])) if row["before_json"] else None,
                "after": json.loads(str(row["after_json"])) if row["after_json"] else None,
            }
            for row in rows
        ]

    def connector_admin_state(self) -> dict[str, list[dict[str, object]]]:
        with self._connect() as conn:
            enrollments = conn.execute(
                """
                SELECT e.id, e.connector_id, e.cluster_id, e.active,
                       EXISTS(SELECT 1 FROM clusters c WHERE c.cluster_id = e.cluster_id) AS registered
                FROM connector_enrollments e ORDER BY e.created_at
                """
            ).fetchall()
            clusters = conn.execute("SELECT * FROM clusters ORDER BY cluster_id").fetchall()
        return {
            "connector_enrollments": [
                {
                    "id": str(row["id"]),
                    "connector_id": str(row["connector_id"]),
                    "cluster_id": str(row["cluster_id"]),
                    "active": bool(row["active"]),
                    "registered": bool(row["registered"]),
                }
                for row in enrollments
            ],
            "clusters": [_cluster_record(row, now=self._clock()) for row in clusters],
        }

    def create_connector_enrollment(
        self,
        *,
        connector_id: str,
        cluster_id: str,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, object], str]:
        connector_id = connector_id.strip()
        cluster_id = cluster_id.strip()
        if not connector_id or not cluster_id:
            raise IdentityError("invalid_enrollment", "connector_id and cluster_id are required")
        now = self._clock()
        enrollment_id = self._id_factory("enr")
        credential = self._credential_factory()
        after = {
            "id": enrollment_id,
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "active": True,
            "registered": False,
        }
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO connector_enrollments (
                        id, connector_id, cluster_id, credential_hash, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (enrollment_id, connector_id, cluster_id, _token_hash(credential), now, now),
                )
                insert_admin_audit(
                    conn,
                    actor_id=actor_id,
                    target_type="connector-enrollments",
                    target_id=enrollment_id,
                    action="connector-enrollments_create",
                    reason=reason,
                    before=None,
                    after=after,
                    result="success",
                    request_id=request_id,
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise IdentityError("enrollment_exists", "Connector or Cluster is already enrolled") from exc
        return after, credential

    def update_connector_enrollment(
        self,
        enrollment_id: str,
        *,
        active: bool | None,
        rotate_credential: bool,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, object], str | None]:
        credential = self._credential_factory() if rotate_credential else None
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            if row is None:
                raise IdentityError("not_found", "Connector Enrollment not found")
            before = _enrollment_record(conn, row)
            next_active = bool(row["active"]) if active is None else active
            conn.execute(
                """
                UPDATE connector_enrollments
                SET credential_hash = ?, active = ?, updated_at = ? WHERE id = ?
                """,
                (_token_hash(credential) if credential else row["credential_hash"], int(next_active), now, enrollment_id),
            )
            if not next_active:
                conn.execute(
                    "UPDATE clusters SET runtime_status = 'offline', failure_summary = 'Connector credential revoked', updated_at = ? WHERE connector_id = ?",
                    (now, row["connector_id"]),
                )
            updated = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            after = _enrollment_record(conn, updated)
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="connector-enrollments",
                target_id=enrollment_id,
                action="connector-enrollments_update",
                reason=reason,
                before=before,
                after=after,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return after, credential

    def register_connector(
        self,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        request_id: str,
    ) -> tuple[dict[str, object], bool]:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            enrollment = _authenticated_enrollment(conn, credential, connector_id, cluster_id)
            previous = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            exists = previous is not None
            conn.execute(
                """
                INSERT INTO clusters (
                    cluster_id, connector_id, display_name, runtime_status, last_heartbeat, created_at, updated_at
                ) VALUES (?, ?, ?, 'offline', ?, ?, ?)
                ON CONFLICT(cluster_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (enrollment["cluster_id"], enrollment["connector_id"], enrollment["cluster_id"], now, now, now),
            )
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            cluster = _cluster_record(row, now=now)
            if not exists:
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connectors",
                    target_id=connector_id,
                    action="connector_register",
                    reason="authenticated Connector registration",
                    before=None,
                    after=cluster,
                    result="success",
                    request_id=request_id,
                )
            conn.commit()
        return cluster, not exists

    def record_connector_heartbeat(
        self,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        status: str,
        failure_summary: str,
        request_id: str,
    ) -> dict[str, object]:
        if status not in {"online", "degraded"}:
            raise IdentityError("invalid_status", "heartbeat status must be online or degraded")
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _authenticated_enrollment(conn, credential, connector_id, cluster_id)
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            if row is None:
                raise IdentityError("not_registered", "Connector must register before heartbeat")
            conn.execute(
                "UPDATE clusters SET runtime_status = ?, failure_summary = ?, last_heartbeat = ?, updated_at = ? WHERE cluster_id = ?",
                (status, failure_summary.strip(), now, now, cluster_id),
            )
            updated = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            cluster = _cluster_record(updated, now=now)
            before = _cluster_record(row, now=now)
            if (before["runtime_status"], before["failure_summary"]) != (
                cluster["runtime_status"],
                cluster["failure_summary"],
            ):
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connectors",
                    target_id=connector_id,
                    action="connector_heartbeat",
                    reason="Connector runtime state transition",
                    before=before,
                    after=cluster,
                    result="success",
                    request_id=request_id,
                )
            conn.commit()
        return cluster

    def authenticate_connector(self, credential: str, connector_id: str, cluster_id: str) -> None:
        with self._connect() as conn:
            _authenticated_enrollment(conn, credential, connector_id, cluster_id)
            if conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone() is None:
                raise IdentityError("not_registered", "Connector must register before discovery")

    def update_cluster(
        self,
        cluster_id: str,
        *,
        payload: dict[str, Any],
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            if row is None:
                raise IdentityError("not_found", "Cluster not found")
            before = _cluster_record(row, now=now)
            display_name = str(payload.get("display_name", row["display_name"])).strip()
            environment = str(payload.get("environment", row["environment"])).strip()
            if not display_name or environment not in {"prod", "staging", "dev", "test"}:
                raise IdentityError("invalid_cluster", "display_name or environment is invalid")
            conn.execute(
                """
                UPDATE clusters SET display_name = ?, environment = ?, governance_notes = ?,
                    mutation_enabled = ?, updated_at = ? WHERE cluster_id = ?
                """,
                (
                    display_name,
                    environment,
                    str(payload.get("governance_notes", row["governance_notes"])).strip(),
                    int(bool(payload.get("mutation_enabled", row["mutation_enabled"]))),
                    now,
                    cluster_id,
                ),
            )
            updated = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            after = _cluster_record(updated, now=now)
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="clusters",
                target_id=cluster_id,
                action="clusters_update",
                reason=reason,
                before=before,
                after=after,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return after

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        with _MIGRATION_LOCK:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in _MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
        conn.commit()
        return conn


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authenticated_enrollment(
    conn: sqlite3.Connection,
    credential: str,
    connector_id: str,
    cluster_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM connector_enrollments WHERE credential_hash = ? AND active = 1",
        (_token_hash(credential),),
    ).fetchone()
    if row is None:
        raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
    if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
        raise IdentityError("identity_mismatch", "Connector identity does not match its Enrollment")
    return row


def _enrollment_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    registered = conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (row["cluster_id"],)).fetchone()
    return {
        "id": str(row["id"]),
        "connector_id": str(row["connector_id"]),
        "cluster_id": str(row["cluster_id"]),
        "active": bool(row["active"]),
        "registered": registered is not None,
    }


def _cluster_record(row: sqlite3.Row, *, now: float) -> dict[str, object]:
    runtime_status = str(row["runtime_status"])
    failure_summary = str(row["failure_summary"])
    if runtime_status != "offline" and now - float(row["last_heartbeat"]) > 120:
        runtime_status = "offline"
        failure_summary = failure_summary or "Connector heartbeat is stale"
    return {
        "cluster_id": str(row["cluster_id"]),
        "connector_id": str(row["connector_id"]),
        "display_name": str(row["display_name"]),
        "environment": str(row["environment"]),
        "governance_notes": str(row["governance_notes"]),
        "mutation_enabled": bool(row["mutation_enabled"]),
        "runtime_status": runtime_status,
        "failure_summary": failure_summary,
        "last_heartbeat": float(row["last_heartbeat"]),
    }


def _active_record(row: sqlite3.Row) -> dict[str, object]:
    value = dict(row)
    value["active"] = bool(value["active"])
    return value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _user_record(conn: sqlite3.Connection, user_id: str) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT id, username, display_name, email, disabled, updated_at
        FROM users WHERE id = ? AND auth_source = 'local'
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise IdentityError("not_found", "user not found")
    return {
        "id": str(row["id"]),
        "username": str(row["username"]),
        "display_name": str(row["display_name"]),
        "email": row["email"],
        "active": not bool(row["disabled"]),
        "updated_at": float(row["updated_at"]),
    }


def _row_record(conn: sqlite3.Connection, table: str, record_id: str, label: str) -> dict[str, object]:
    if table not in {"teams", "team_memberships", "role_bindings"}:
        raise ValueError("unsupported administration table")
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise IdentityError("not_found", f"{label} not found")
    return _active_record(row)


def _is_platform_admin(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM role_bindings WHERE user_id = ? AND role = 'platform_administrator' AND active = 1",
        (user_id,),
    ).fetchone() is not None


def _active_admin_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT rb.user_id)
            FROM role_bindings rb
            JOIN users u ON u.id = rb.user_id
            WHERE rb.role = 'platform_administrator' AND rb.active = 1 AND u.disabled = 0
            """
        ).fetchone()[0]
    )
