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
_EXTRA_COLUMNS = {
    "payload_json": "TEXT",
}

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
    payload_json TEXT,
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
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """兼容已存在的 message_deliveries 数据库。"""
        for column, definition in _EXTRA_COLUMNS.items():
            try:
                self._conn.execute(f"ALTER TABLE message_deliveries ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

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
        payload: dict[str, Any] | None = None,
    ) -> str:
        delivery_id = str(uuid.uuid4())
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else None

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
                        payload_json = COALESCE(?, payload_json),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (approval_id, platform, chat_id, thread_id, payload_json, now, existing["id"]),
                )
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO message_deliveries (
                    id, incident_id, approval_id, target_type, platform, chat_id, thread_id,
                    target_message_id, delivery_status, delivery_attempts, last_delivery_error,
                    last_delivery_at, payload_hash, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'pending', 0, NULL, NULL, ?, ?, ?, ?)
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
                    payload_json,
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

    async def find_sent_delivery_for_approval(
        self,
        *,
        approval_id: str,
        target_type: str,
    ) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            return self._fetchone(
                """
                SELECT *
                FROM message_deliveries
                WHERE approval_id = ?
                  AND target_type = ?
                  AND delivery_status = 'sent'
                  AND target_message_id IS NOT NULL
                  AND target_message_id != ''
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (approval_id, target_type),
            )

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
    payload: dict[str, Any] | None = None,
) -> str:
    return await _DB.upsert_delivery(
        incident_id=incident_id,
        target_type=target_type,
        platform=platform,
        chat_id=chat_id,
        payload_hash=payload_hash,
        approval_id=approval_id,
        thread_id=thread_id,
        payload=payload,
    )


async def get_delivery(delivery_id: str) -> dict[str, Any]:
    return await _DB.get_delivery(delivery_id)


async def find_sent_delivery_for_approval(
    *,
    approval_id: str,
    target_type: str,
) -> dict[str, Any] | None:
    return await _DB.find_sent_delivery_for_approval(
        approval_id=approval_id,
        target_type=target_type,
    )


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


def build_approval_execution_notification(
    *,
    incident_id: str,
    event_type: str,
    approval: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造审批执行终态通知 payload。"""

    approval = approval or {}
    execution = execution or {}
    action = action or {}
    target_type = _approval_execution_target_type(event_type, execution)
    status = str(execution.get("status") or _status_from_event_type(event_type) or "unknown")
    action_ref = _format_action_ref(action or execution)
    approval_id = str(approval.get("approval_id") or execution.get("approval_id") or "")
    execution_id = str(execution.get("id") or "")
    health_result = execution.get("health_result") if isinstance(execution.get("health_result"), dict) else {}
    reason_code = str(
        execution.get("reason_code")
        or (health_result or {}).get("reason_code")
        or ("execution_succeeded" if target_type == "approval_execution_succeeded" else "execution_failed")
    )
    summary = _approval_execution_summary(target_type, execution, health_result)
    audit_id = execution.get("audit_id")

    lines = [
        _approval_execution_title(target_type, status),
        f"Incident: {incident_id}",
    ]
    if approval_id:
        lines.append(f"Approval: {approval_id}")
    if execution_id:
        lines.append(f"Execution: {execution_id}")
    lines.extend(
        [
            f"Status: {status}",
            f"Action: {action_ref}",
            f"Reason: {reason_code}",
            f"Summary: {summary}",
        ]
    )
    if audit_id not in (None, ""):
        lines.append(f"Audit: {audit_id}")

    return {
        "msg_type": "text",
        "target_type": target_type,
        "content": {"text": "\n".join(lines)},
        "metadata": {
            "incident_id": incident_id,
            "approval_id": approval_id or None,
            "execution_id": execution_id or None,
            "event_type": event_type,
            "execution_status": status,
            "action_type": action.get("action_type") or execution.get("action_type"),
            "namespace": action.get("namespace") or execution.get("namespace"),
            "resource_name": action.get("resource_name") or execution.get("resource_name"),
            "reason_code": reason_code,
            "audit_id": audit_id,
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
        payload=payload,
    )
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "target_type": "rollback_required",
        "payload_hash": payload_hash,
        "payload": payload,
    }


async def queue_approval_execution_notification(
    *,
    incident_id: str,
    platform: str,
    chat_id: str,
    event_type: str,
    thread_id: str | None = None,
    approval_id: str | None = None,
    approval: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记审批执行终态通知，等待发送或补偿。"""

    payload = build_approval_execution_notification(
        incident_id=incident_id,
        event_type=event_type,
        approval=approval,
        execution=execution,
        action=action,
    )
    payload_hash = _stable_payload_hash(payload)
    delivery_id = await upsert_delivery(
        incident_id=incident_id,
        target_type=str(payload["target_type"]),
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        approval_id=approval_id,
        payload_hash=payload_hash,
        payload=payload,
    )
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "target_type": payload["target_type"],
        "payload_hash": payload_hash,
        "payload": payload,
    }


def _format_action_ref(action: dict[str, Any]) -> str:
    action_type = str(action.get("action_type") or "unknown_action")
    namespace = str(action.get("namespace") or "unknown_namespace")
    resource_kind = str(action.get("resource_kind") or "resource")
    resource_name = str(action.get("resource_name") or "unknown")
    return f"{action_type} {namespace}/{resource_kind}/{resource_name}"


def _approval_execution_target_type(event_type: str, execution: dict[str, Any]) -> str:
    if event_type == "approval_execution_succeeded":
        return "approval_execution_succeeded"
    if event_type == "approval_execution_rollback_required" or execution.get("status") == "rollback_required":
        return "rollback_required"
    return "approval_execution_failed"


def _status_from_event_type(event_type: str) -> str | None:
    prefix = "approval_execution_"
    if event_type.startswith(prefix):
        return event_type[len(prefix):]
    return None


def _approval_execution_title(target_type: str, status: str) -> str:
    if target_type == "approval_execution_succeeded":
        return "自动修复执行成功。"
    if target_type == "rollback_required":
        return "自动修复健康检查失败，需要人工判断 rollback。"
    if status == "dry_run_failed":
        return "自动修复 dry-run 失败，未执行真实变更。"
    return "自动修复执行失败。"


def _approval_execution_summary(
    target_type: str,
    execution: dict[str, Any],
    health_result: dict[str, Any],
) -> str:
    if health_result.get("summary"):
        return str(health_result["summary"])
    if execution.get("error_message"):
        return str(execution["error_message"])
    if target_type == "approval_execution_succeeded":
        return "执行、审计与健康检查已完成"
    if target_type == "rollback_required":
        return "健康检查未通过"
    return "执行链路失败"


def _stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
