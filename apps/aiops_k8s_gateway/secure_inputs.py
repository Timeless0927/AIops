"""Gateway owner for encrypted, short-lived Secure Input values."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from aiops.security import (
    DEFAULT_CHANGE_KEY_PATH,
    SecureInputCryptoError,
    decrypt_secure_input_refs,
    encrypt_secure_input,
    key_fingerprint,
    public_secure_input_facts,
    public_secure_input_ref,
    read_change_encryption_key,
    secure_input_ids,
    secure_input_placeholder,
    value_hash,
)

from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations


_SCHEMA_VERSION = 30
_SCHEMA = """
CREATE TABLE secure_inputs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES users(id),
    key_name TEXT NOT NULL,
    placeholder TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('user', 'generated')),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    key_fingerprint TEXT NOT NULL CHECK (length(key_fingerprint) = 64),
    nonce BLOB,
    ciphertext BLOB,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    cleanup_after REAL,
    deleted_at REAL,
    UNIQUE(actor_id, idempotency_key),
    CHECK ((nonce IS NULL) = (ciphertext IS NULL)),
    CHECK ((ciphertext IS NULL) = (deleted_at IS NOT NULL))
);
CREATE INDEX secure_inputs_cleanup ON secure_inputs(cleanup_after) WHERE ciphertext IS NOT NULL;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_HOLD_SCHEMA_VERSION = 33
_HOLD_SCHEMA = """
CREATE TABLE secure_input_revision_holds (
    secure_input_id TEXT NOT NULL REFERENCES secure_inputs(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    released_at REAL,
    PRIMARY KEY (secure_input_id, revision_id)
);
CREATE INDEX secure_input_active_holds
    ON secure_input_revision_holds(secure_input_id) WHERE released_at IS NULL;
"""
register_migrations(((_HOLD_SCHEMA_VERSION, _HOLD_SCHEMA),))

_KEY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class SecureInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SecureInputs:
    """Creates encrypted values and resolves only encrypted transport refs."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._key_path = Path(key_path)
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._random_bytes = random_bytes
        self._ttl_seconds = ttl_seconds

    def create(
        self,
        *,
        actor_id: str,
        key_name: str,
        value: object,
        generated_bytes: object,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        actor_id = _text(actor_id, "actor_id", 200)
        key_name = _text(key_name, "key_name", 128)
        if _KEY_NAME.fullmatch(key_name) is None:
            raise SecureInputError("invalid_request", "key_name is invalid")
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request_id = _text(request_id, "request_id", 200)
        if (value is None) == (generated_bytes is None):
            raise SecureInputError("invalid_request", "exactly one Secure Input source is required")
        if value is not None:
            plaintext = _text(value, "value", 64 * 1024)
            source = "user"
            generated_size = None
            request_value_hash = value_hash(plaintext)
        else:
            if isinstance(generated_bytes, bool) or not isinstance(generated_bytes, int) or not 16 <= generated_bytes <= 64:
                raise SecureInputError("invalid_request", "generated_bytes must be between 16 and 64")
            plaintext = None
            source = "generated"
            generated_size = generated_bytes
            request_value_hash = None
        request_hash = _digest({
            "actor_id": actor_id, "key_name": key_name, "source": source,
            "sha256": request_value_hash, "generated_bytes": generated_size,
        })
        now = self._clock()
        with self._database.connect() as conn:
            replay = conn.execute(
                "SELECT * FROM secure_inputs WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise SecureInputError("idempotency_conflict", "Idempotency key was used for another Secure Input")
                return self._project(replay, idempotent=True)
            if plaintext is None:
                assert generated_size is not None
                plaintext = base64.urlsafe_b64encode(
                    self._random_bytes(generated_size),
                ).rstrip(b"=").decode("ascii")
            digest = value_hash(plaintext)
            try:
                key = read_change_encryption_key(self._key_path)
            except SecureInputCryptoError as exc:
                raise SecureInputError("secure_input_unavailable", str(exc)) from exc
            input_id = self._id_factory()
            try:
                placeholder = secure_input_placeholder(input_id)
            except SecureInputCryptoError as exc:
                raise SecureInputError("invalid_secure_input_id", str(exc)) from exc
            nonce = self._random_bytes(12)
            ciphertext, encrypted_digest, fingerprint = encrypt_secure_input(
                input_id=input_id, key_name=key_name, value=plaintext, key=key, nonce=nonce,
            )
            assert encrypted_digest == digest
            conn.execute(
                """
                INSERT INTO secure_inputs (
                    id, actor_id, key_name, placeholder, source, sha256, key_fingerprint,
                    nonce, ciphertext, idempotency_key, request_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_id, actor_id, key_name, placeholder, source, digest, fingerprint,
                    nonce, ciphertext, idempotency_key, request_hash, now, now + self._ttl_seconds,
                ),
            )
            row = conn.execute("SELECT * FROM secure_inputs WHERE id = ?", (input_id,)).fetchone()
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="secure_inputs",
                target_id=input_id,
                action="secure_input_create",
                reason="Secure Input created",
                before=None,
                after={
                    "key_name": key_name,
                    "sha256": digest,
                    "source": source,
                },
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return self._project(row, idempotent=False)

    def get(self, input_id: str, *, actor_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM secure_inputs WHERE id = ? AND actor_id = ?", (input_id, actor_id),
            ).fetchone()
        if row is None:
            raise SecureInputError("not_found", "Secure Input not found")
        return self._project(row, idempotent=False)

    def encrypted_refs_for_value_in(
        self, conn: sqlite3.Connection, *, actor_id: str, value: object,
    ) -> list[dict[str, object]]:
        ids = secure_input_ids(value)
        if not ids:
            return []
        rows = conn.execute(
            f"SELECT * FROM secure_inputs WHERE actor_id = ? AND id IN ({','.join('?' for _ in ids)})",
            (actor_id, *ids),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if set(ids) != set(by_id):
            raise SecureInputError("secure_input_unavailable", "Secure Input is unavailable")
        refs = [self._encrypted_ref(by_id[input_id]) for input_id in ids]
        try:
            decrypt_secure_input_refs(refs, key_path=self._key_path)
        except SecureInputCryptoError as exc:
            raise SecureInputError("secure_input_unavailable", str(exc)) from exc
        return refs

    def public_refs_for_ids_in(
        self, conn: sqlite3.Connection, input_ids: list[str],
    ) -> list[dict[str, str]]:
        rows = self._rows_for_ids_in(conn, input_ids)
        ordered = sorted(rows, key=lambda item: (str(item["key_name"]), str(item["id"])))
        return public_secure_input_facts([dict(row) for row in ordered])

    def execution_refs_for_ids_in(
        self, conn: sqlite3.Connection, input_ids: list[str],
    ) -> list[dict[str, str]]:
        rows = self._rows_for_ids_in(conn, input_ids)
        return [
            public_secure_input_ref(dict(row))
            for row in sorted(rows, key=lambda item: (str(item["key_name"]), str(item["id"])))
        ]

    @staticmethod
    def _rows_for_ids_in(
        conn: sqlite3.Connection, input_ids: list[str],
    ) -> list[sqlite3.Row]:
        if not input_ids:
            return []
        if len(set(input_ids)) != len(input_ids):
            raise SecureInputError("secure_input_unavailable", "Secure Input metadata is invalid")
        rows = conn.execute(
            f"SELECT * FROM secure_inputs WHERE id IN ({','.join('?' for _ in input_ids)})",
            input_ids,
        ).fetchall()
        if {str(row["id"]) for row in rows} != set(input_ids):
            raise SecureInputError("secure_input_unavailable", "Secure Input is unavailable")
        return rows

    def encrypted_refs_for_metadata_in(
        self, conn: sqlite3.Connection, metadata: object,
    ) -> list[dict[str, object]]:
        if not isinstance(metadata, list):
            raise SecureInputError("secure_input_unavailable", "Secure Input metadata is invalid")
        ids = [str(item.get("id")) for item in metadata if isinstance(item, dict)]
        if len(ids) != len(metadata) or len(set(ids)) != len(ids):
            raise SecureInputError("secure_input_unavailable", "Secure Input metadata is invalid")
        if not ids:
            return []
        rows = conn.execute(
            f"SELECT * FROM secure_inputs WHERE id IN ({','.join('?' for _ in ids)})", ids,
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if set(ids) != set(by_id):
            raise SecureInputError("secure_input_unavailable", "Secure Input is unavailable")
        refs = [self._encrypted_ref(by_id[input_id]) for input_id in ids]
        try:
            decrypt_secure_input_refs(refs, key_path=self._key_path)
        except SecureInputCryptoError as exc:
            raise SecureInputError("secure_input_unavailable", str(exc)) from exc
        return refs

    def schedule_cleanup_in(
        self, conn: sqlite3.Connection, input_ids: list[str], *, delete_after: float,
    ) -> None:
        if not input_ids:
            return
        conn.execute(
            f"""
            UPDATE secure_inputs
            SET cleanup_after = CASE
                WHEN cleanup_after IS NULL OR cleanup_after < ? THEN ? ELSE cleanup_after END
            WHERE id IN ({','.join('?' for _ in input_ids)}) AND ciphertext IS NOT NULL
            """,
            (delete_after, delete_after, *input_ids),
        )

    def hold_revision_in(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        refs: list[dict[str, object]],
    ) -> None:
        conn.executemany(
            "INSERT INTO secure_input_revision_holds "
            "(secure_input_id, revision_id) VALUES (?, ?) "
            "ON CONFLICT(secure_input_id, revision_id) DO UPDATE SET released_at = NULL",
            [(str(ref["id"]), revision_id) for ref in refs],
        )

    def release_revision_in(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        released_at: float,
        delete_after: float,
    ) -> None:
        rows = conn.execute(
            "SELECT secure_input_id FROM secure_input_revision_holds "
            "WHERE revision_id = ? AND released_at IS NULL",
            (revision_id,),
        ).fetchall()
        input_ids = [str(row["secure_input_id"]) for row in rows]
        conn.execute(
            "UPDATE secure_input_revision_holds SET released_at = ? "
            "WHERE revision_id = ? AND released_at IS NULL",
            (released_at, revision_id),
        )
        self.schedule_cleanup_in(conn, input_ids, delete_after=delete_after)

    def cleanup_expired(self, *, now: float | None = None) -> int:
        observed_at = self._clock() if now is None else now
        with self._database.connect() as conn:
            deleted = conn.execute(
                """
                UPDATE secure_inputs SET nonce = NULL, ciphertext = NULL, deleted_at = ?
                WHERE ciphertext IS NOT NULL
                  AND (
                    expires_at <= ?
                    OR (
                      cleanup_after IS NOT NULL AND cleanup_after <= ?
                      AND NOT EXISTS (
                        SELECT 1 FROM secure_input_revision_holds active_hold
                        WHERE active_hold.secure_input_id = secure_inputs.id
                          AND active_hold.released_at IS NULL
                      )
                    )
                  )
                """,
                (observed_at, observed_at, observed_at),
            ).rowcount
            conn.commit()
        return deleted

    def _encrypted_ref(self, row: sqlite3.Row) -> dict[str, object]:
        if (
            row["ciphertext"] is None or row["nonce"] is None
            or float(row["expires_at"]) <= self._clock()
        ):
            raise SecureInputError("secure_input_unavailable", "Secure Input is unavailable")
        return {
            "id": str(row["id"]), "key_name": str(row["key_name"]),
            "placeholder": str(row["placeholder"]), "sha256": str(row["sha256"]),
            "key_fingerprint": str(row["key_fingerprint"]),
            "nonce": base64.urlsafe_b64encode(bytes(row["nonce"])).decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(bytes(row["ciphertext"])).decode("ascii"),
        }

    def _project(self, row: sqlite3.Row, *, idempotent: bool) -> dict[str, object]:
        available = row["ciphertext"] is not None and float(row["expires_at"]) > self._clock()
        if available:
            try:
                available = key_fingerprint(read_change_encryption_key(self._key_path)) == row["key_fingerprint"]
            except SecureInputCryptoError:
                available = False
        return {
            "id": str(row["id"]), "key_name": str(row["key_name"]),
            "placeholder": str(row["placeholder"]), "sha256": str(row["sha256"]),
            "source": str(row["source"]), "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]), "available": available,
            "idempotent": idempotent,
        }


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized.encode("utf-8")) > limit:
        raise SecureInputError("invalid_request", f"{field} is required")
    return normalized


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
