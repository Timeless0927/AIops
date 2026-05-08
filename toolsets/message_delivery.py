"""消息投递补偿存储。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_deliveries (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    approval_id TEXT,
    target_type TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    target_message_id TEXT,
    delivery_status TEXT NOT NULL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_delivery_error TEXT,
    last_delivery_at REAL,
    payload_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(incident_id, target_type, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_message_deliveries_status
ON message_deliveries(delivery_status, updated_at);
"""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "message_deliveries.db"
    return _project_root() / "data" / "message_deliveries.db"


class MessageDeliveryDB:
    """基于 SQLite 的消息投递补偿存储。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Exception | None = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    if self._conn is None:
                        raise sqlite3.ProgrammingError("数据库连接已关闭")
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if ("locked" in message or "busy" in message) and attempt < _WRITE_MAX_RETRIES - 1:
                    last_err = exc
                    time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                    continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def upsert_delivery(
        self,
        *,
        incident_id: str,
        target_type: str,
        platform: str,
        chat_id: str,
        payload_hash: str,
        approval_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        delivery_id = str(uuid.uuid4())
        now = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            existing = conn.execute(
                """
                SELECT id FROM message_deliveries
                WHERE incident_id = ? AND target_type = ? AND payload_hash = ?
                """,
                (incident_id, target_type, payload_hash),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE message_deliveries
                    SET approval_id = ?,
                        platform = ?,
                        chat_id = ?,
                        thread_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (approval_id, platform, chat_id, thread_id, now, existing["id"]),
                )
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO message_deliveries (
                    id, incident_id, approval_id, target_type, platform, chat_id, thread_id,
                    target_message_id, delivery_status, delivery_attempts, last_delivery_error,
                    last_delivery_at, payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'pending', 0, NULL, NULL, ?, ?, ?)
                """,
                (
                    delivery_id,
                    incident_id,
                    approval_id,
                    target_type,
                    platform,
                    chat_id,
                    thread_id,
                    payload_hash,
                    now,
                    now,
                ),
            )
            return delivery_id

        return await asyncio.to_thread(self._execute_write, _write)

    async def get_delivery(self, delivery_id: str) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            row = self._fetchone("SELECT * FROM message_deliveries WHERE id = ?", (delivery_id,))
            if row is None:
                raise ValueError(f"消息投递记录不存在: {delivery_id}")
            return row

        return await asyncio.to_thread(_read)

    async def mark_failed(self, delivery_id: str, error: str) -> None:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE message_deliveries
                SET delivery_status = 'failed',
                    delivery_attempts = delivery_attempts + 1,
                    last_delivery_error = ?,
                    last_delivery_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, now, delivery_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"消息投递记录不存在: {delivery_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def mark_sent(self, delivery_id: str, target_message_id: str) -> None:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE message_deliveries
                SET delivery_status = 'sent',
                    target_message_id = ?,
                    last_delivery_error = NULL,
                    last_delivery_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (target_message_id, now, now, delivery_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"消息投递记录不存在: {delivery_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            return self._fetchall(
                """
                SELECT *
                FROM message_deliveries
                WHERE delivery_status IN ('pending', 'failed')
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            )

        return await asyncio.to_thread(_read)


_DB = MessageDeliveryDB()


async def upsert_delivery(
    *,
    incident_id: str,
    target_type: str,
    platform: str,
    chat_id: str,
    payload_hash: str,
    approval_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    return await _DB.upsert_delivery(
        incident_id=incident_id,
        target_type=target_type,
        platform=platform,
        chat_id=chat_id,
        payload_hash=payload_hash,
        approval_id=approval_id,
        thread_id=thread_id,
    )


async def get_delivery(delivery_id: str) -> dict[str, Any]:
    return await _DB.get_delivery(delivery_id)


async def mark_failed(delivery_id: str, error: str) -> None:
    await _DB.mark_failed(delivery_id, error)


async def mark_sent(delivery_id: str, target_message_id: str) -> None:
    await _DB.mark_sent(delivery_id, target_message_id)


async def list_pending(limit: int = 100) -> list[dict[str, Any]]:
    return await _DB.list_pending(limit)


def build_rollback_required_notification(
    *,
    incident_id: str,
    action: dict[str, Any] | None = None,
    health_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 rollback_required 人工通知 payload。"""

    action = action or {}
    health_result = health_result or {}
    action_ref = _format_action_ref(action)
    reason_code = str(health_result.get("reason_code") or "health_check_failed")
    summary = str(health_result.get("summary") or "执行后健康检查失败")
    text = (
        "自动修复健康检查失败，需要人工判断 rollback。\n"
        f"Incident: {incident_id}\n"
        f"Action: {action_ref}\n"
        f"Reason: {reason_code}\n"
        f"Summary: {summary}"
    )
    return {
        "msg_type": "text",
        "target_type": "rollback_required",
        "content": {"text": text},
        "metadata": {
            "incident_id": incident_id,
            "action_type": action.get("action_type"),
            "namespace": action.get("namespace"),
            "resource_name": action.get("resource_name"),
            "reason_code": reason_code,
        },
    }


async def queue_rollback_required_notification(
    *,
    incident_id: str,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    approval_id: str | None = None,
    action: dict[str, Any] | None = None,
    health_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记 rollback_required 通知，等待现有投递补偿流程发送。"""

    payload = build_rollback_required_notification(
        incident_id=incident_id,
        action=action,
        health_result=health_result,
    )
    payload_hash = _stable_payload_hash(payload)
    delivery_id = await upsert_delivery(
        incident_id=incident_id,
        target_type="rollback_required",
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        approval_id=approval_id,
        payload_hash=payload_hash,
    )
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "target_type": "rollback_required",
        "payload_hash": payload_hash,
        "payload": payload,
    }


def _format_action_ref(action: dict[str, Any]) -> str:
    action_type = str(action.get("action_type") or "unknown_action")
    namespace = str(action.get("namespace") or "unknown_namespace")
    resource_kind = str(action.get("resource_kind") or "resource")
    resource_name = str(action.get("resource_name") or "unknown")
    return f"{action_type} {namespace}/{resource_kind}/{resource_name}"


def _stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
