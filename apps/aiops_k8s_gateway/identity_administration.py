"""Gateway identity administration mutations and invariants."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiops.domain.identity import IdentityError, hash_password

from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase
from .gateway_sessions import GatewaySessions


class IdentityAdministration:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        audit_insert: Callable[..., None] = insert_admin_audit,
        revoke_actor_in: Callable[[sqlite3.Connection, str], None] = GatewaySessions.revoke_actor_in,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._audit_insert = audit_insert
        self._revoke_actor_in = revoke_actor_in
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def ensure_platform_administrator(self, user_id: str) -> None:
        now = self._clock()
        with self._database.connect() as conn:
            if not _table_exists(conn, "users") or conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO role_bindings (
                    id, user_id, role, scope_type, scope_id, active, created_at, updated_at
                ) VALUES (?, ?, 'platform_administrator', 'platform', NULL, 1, ?, ?)
                """,
                (self._id_factory("rb"), user_id, now, now),
            )

    @staticmethod
    def user_active_in(conn: sqlite3.Connection, user_id: str) -> bool:
        row = conn.execute("SELECT disabled FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and not bool(row["disabled"])

    def state(self) -> dict[str, list[dict[str, object]]]:
        with self._database.connect() as conn:
            users = conn.execute(
                """
                SELECT id, username, display_name, email, disabled, updated_at
                FROM users WHERE auth_source = 'local' ORDER BY username
                """
            ).fetchall()
            teams = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
            memberships = conn.execute("SELECT * FROM team_memberships ORDER BY created_at").fetchall()
            bindings = conn.execute("SELECT * FROM role_bindings ORDER BY created_at").fetchall()
        return {
            "users": [_user_view(row) for row in users],
            "teams": [_active_record(row) for row in teams],
            "team_memberships": [_active_record(row) for row in memberships],
            "role_bindings": [_active_record(row) for row in bindings],
        }

    def mutate(
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
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mutation_complete = False
            try:
                response_key, audit_target, before, after = self._mutate(conn, collection, target_id, payload)
                mutation_complete = True
                self._audit_insert(
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

    def _mutate(
        self,
        conn: sqlite3.Connection,
        collection: str,
        target_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, object] | None, dict[str, object]]:
        now = self._clock()
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
                target_id = self._id_factory("usr")
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, email, disabled, created_at,
                        updated_at, password, auth_source
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'local')
                    """,
                    (target_id, username, display_name, payload.get("email"), now, now, hash_password(password)),
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
                self._revoke_actor_in(conn, target_id)
            return "user", target_id, before, _user_record(conn, target_id)

        if collection == "teams":
            if target_id is None:
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise IdentityError("invalid_team", "team name is required")
                target_id = self._id_factory("team")
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
                actor_ids = conn.execute("SELECT user_id FROM team_memberships WHERE team_id = ?", (target_id,)).fetchall()
                for row in actor_ids:
                    self._revoke_actor_in(conn, str(row["user_id"]))
            return "team", target_id, before, _row_record(conn, "teams", target_id, "team")

        if collection == "team-memberships":
            if target_id is None:
                user_id = str(payload.get("user_id") or "")
                team_id = str(payload.get("team_id") or "")
                _user_record(conn, user_id)
                _row_record(conn, "teams", team_id, "team")
                target_id = self._id_factory("tm")
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
                self._revoke_actor_in(conn, str(before["user_id"]))
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
                target_id = self._id_factory("rb")
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
                self._revoke_actor_in(conn, str(before["user_id"]))
            return "role_binding", target_id, before, _row_record(conn, "role_bindings", target_id, "role binding")

        raise IdentityError("not_found", "administration resource not found")


def _active_record(row: sqlite3.Row) -> dict[str, object]:
    value = dict(row)
    value["active"] = bool(value["active"])
    return value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _user_view(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "username": str(row["username"]),
        "display_name": str(row["display_name"]),
        "email": row["email"],
        "active": not bool(row["disabled"]),
        "updated_at": float(row["updated_at"]),
    }


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
    return _user_view(row)


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
