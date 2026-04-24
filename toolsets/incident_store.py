"""事件时间线持久化工具。"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    from hermes_agent.tools.registry import registry  # type: ignore


T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50
_INCIDENT_EXTRA_COLUMNS = {
    "operator": "TEXT",
    "closed_at": "REAL",
    "platform": "TEXT",
    "chat_id": "TEXT",
    "root_message_id": "TEXT",
    "thread_id": "TEXT",
    "status_card_message_id": "TEXT",
    "dedup_key": "TEXT",
    "dedup_key_version": "TEXT",
    "reopen_count": "INTEGER NOT NULL DEFAULT 0",
}
TERMINAL_STATUSES = {"resolved", "closed"}
ACTIVE_STATUSES = {"new", "triaging", "investigating", "pending_approval", "executing", "verifying", "abnormal"}
_ALLOWED_TRANSITIONS = {
    "new": {"triaging", "resolved", "abnormal"},
    "triaging": {"investigating", "resolved", "abnormal"},
    "investigating": {"pending_approval", "executing", "resolved", "abnormal"},
    "pending_approval": {"investigating", "executing", "abnormal"},
    "executing": {"verifying", "abnormal"},
    "verifying": {"resolved", "investigating", "abnormal"},
    "resolved": {"triaging", "closed"},
    "closed": set(),
    "abnormal": {"triaging", "investigating", "closed"},
}
_VALID_EVENT_TYPES = {
    "alert_fired",
    "reopened",
    "triage_start",
    "triage_end",
    "investigate_start",
    "investigate_end",
    "remediate_proposed",
    "approval_sent",
    "approval_received",
    "remediate_executed",
    "remediate_verified",
    "resolved",
    "postmortem_start",
    "postmortem_end",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    alert_name TEXT NOT NULL,
    namespace TEXT,
    cluster TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    closed_at REAL,
    summary TEXT,
    operator TEXT,
    platform TEXT,
    chat_id TEXT,
    root_message_id TEXT,
    thread_id TEXT,
    status_card_message_id TEXT,
    dedup_key TEXT,
    dedup_key_version TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    tool_name TEXT,
    input_summary TEXT,
    output_summary TEXT,
    metadata_json TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incident_events_incident_id ON incident_events(incident_id, timestamp, id);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    """返回默认数据库路径。"""
    return _project_root() / "data" / "incidents.db"


class IncidentStore:
    """基于 SQLite 的事件时间线存储。"""

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
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._ensure_incident_columns()
        self._ensure_incident_indexes()

    def _ensure_incident_columns(self) -> None:
        """兼容已存在数据库，补齐 incident 扩展列。"""
        for column, definition in _INCIDENT_EXTRA_COLUMNS.items():
            try:
                self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

    def _ensure_incident_indexes(self) -> None:
        """兼容迁移完成后创建 incident 扩展索引。"""
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_dedup "
            "ON incidents(dedup_key, dedup_key_version, status, created_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_thread "
            "ON incidents(platform, chat_id, thread_id)"
        )

    def close(self) -> None:
        """关闭数据库连接。"""
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
        """执行带重试的写事务。"""
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
        """尽力执行被动 checkpoint。"""
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """执行查询并返回字典列表。"""
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """执行查询并返回单行字典。"""
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def create_incident(
        self,
        alert_name: str,
        namespace: str,
        cluster: str,
        summary: str,
        *,
        platform: str | None = None,
        chat_id: str | None = None,
        root_message_id: str | None = None,
        thread_id: str | None = None,
        status_card_message_id: str | None = None,
        dedup_key: str | None = None,
        dedup_key_version: str | None = None,
    ) -> str:
        """创建事件并返回事件 ID。"""
        incident_id = str(uuid.uuid4())
        created_at = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO incidents (
                    id, alert_name, namespace, cluster, status, created_at, resolved_at, closed_at,
                    summary, platform, chat_id, root_message_id, thread_id, status_card_message_id,
                    dedup_key, dedup_key_version, reopen_count
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    incident_id,
                    alert_name,
                    namespace,
                    cluster,
                    "new",
                    created_at,
                    summary,
                    platform,
                    chat_id,
                    root_message_id,
                    thread_id,
                    status_card_message_id,
                    dedup_key,
                    dedup_key_version,
                ),
            )
            return incident_id

        return await asyncio.to_thread(self._execute_write, _write)

    async def get_incident(self, incident_id: str) -> dict[str, Any]:
        """读取事件主记录。"""

        def _read() -> dict[str, Any]:
            row = self._fetchone("SELECT * FROM incidents WHERE id = ?", (incident_id,))
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            return row

        return await asyncio.to_thread(_read)

    async def add_event(
        self,
        incident_id: str,
        event_type: str,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """为事件追加时间线记录。"""
        if event_type not in _VALID_EVENT_TYPES:
            raise ValueError(f"不支持的 event_type: {event_type}")

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        timestamp = time.time()

        def _write(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            cursor = conn.execute(
                """
                INSERT INTO incident_events (
                    incident_id, event_type, timestamp, tool_name, input_summary, output_summary, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (incident_id, event_type, timestamp, tool_name, input_summary, output_summary, metadata_json),
            )
            return int(cursor.lastrowid)

        return await asyncio.to_thread(self._execute_write, _write)

    async def get_timeline(self, incident_id: str) -> list[dict[str, Any]]:
        """读取完整时间线。"""

        def _read() -> list[dict[str, Any]]:
            rows = self._fetchall(
                """
                SELECT id, incident_id, event_type, timestamp, tool_name, input_summary, output_summary, metadata_json
                FROM incident_events
                WHERE incident_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (incident_id,),
            )
            for row in rows:
                metadata_json = row.get("metadata_json") or "{}"
                row["metadata"] = json.loads(metadata_json)
            return rows

        return await asyncio.to_thread(_read)

    async def update_status(
        self,
        incident_id: str,
        status: str,
        resolved_at: float | None = None,
        closed_at: float | None = None,
    ) -> None:
        """更新事件状态。"""

        if status not in _ALLOWED_TRANSITIONS:
            raise ValueError(f"不支持的 incident status: {status}")

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT status, resolved_at, closed_at FROM incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")

            current_status = str(row["status"])
            if current_status not in _ALLOWED_TRANSITIONS:
                raise ValueError(f"不支持的 incident status: {current_status}")
            if status not in _ALLOWED_TRANSITIONS[current_status]:
                raise ValueError(f"非法状态迁移: {current_status} -> {status}")

            cursor = conn.execute(
                "UPDATE incidents SET status = ?, resolved_at = ?, closed_at = ? WHERE id = ?",
                (
                    status,
                    resolved_at if resolved_at is not None else row["resolved_at"],
                    closed_at if closed_at is not None else row["closed_at"],
                    incident_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"事件不存在: {incident_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def update_operator(self, incident_id: str, operator: str) -> None:
        """更新事件负责人。"""

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE incidents SET operator = ? WHERE id = ?",
                (operator, incident_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"事件不存在: {incident_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def list_active(self) -> list[dict[str, Any]]:
        """列出未完成事件。"""

        def _read() -> list[dict[str, Any]]:
            return self._fetchall(
                """
                SELECT *
                FROM incidents
                WHERE status NOT IN ('resolved', 'closed')
                ORDER BY created_at DESC
                """
            )

        return await asyncio.to_thread(_read)

    async def find_reusable_incident(self, dedup_key: str, dedup_key_version: str) -> dict[str, Any] | None:
        """按 dedup key 查找可复用 incident。"""

        def _read() -> dict[str, Any] | None:
            return self._fetchone(
                """
                SELECT *
                FROM incidents
                WHERE dedup_key = ? AND dedup_key_version = ? AND status != 'closed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (dedup_key, dedup_key_version),
            )

        return await asyncio.to_thread(_read)

    async def reopen_incident(self, incident_id: str, reason: str) -> dict[str, Any]:
        """将已恢复事件重新打开，并写入 reopened 时间线。"""
        timestamp = time.time()

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            if row["status"] != "resolved":
                raise ValueError(f"仅支持重开 resolved 事件: {incident_id}")

            reopen_count = int(row["reopen_count"] or 0) + 1
            conn.execute(
                """
                UPDATE incidents
                SET status = 'triaging',
                    closed_at = NULL,
                    reopen_count = ?
                WHERE id = ?
                """,
                (reopen_count, incident_id),
            )
            conn.execute(
                """
                INSERT INTO incident_events (
                    incident_id, event_type, timestamp, tool_name, input_summary, output_summary, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    "reopened",
                    timestamp,
                    "incident_store",
                    "incident reopen",
                    reason,
                    json.dumps({}, ensure_ascii=False, sort_keys=True),
                ),
            )

            updated = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if updated is None:
                raise ValueError(f"事件不存在: {incident_id}")
            return dict(updated)

        return await asyncio.to_thread(self._execute_write, _write)


_STORE = IncidentStore()


INCIDENT_CREATE_SCHEMA = {
    "name": "incident_create",
    "description": "创建新的 SRE 事件并返回事件 ID。",
    "parameters": {
        "type": "object",
        "properties": {
            "alert_name": {"type": "string", "description": "告警名称"},
            "namespace": {"type": "string", "description": "命名空间"},
            "cluster": {"type": "string", "description": "集群名称"},
            "summary": {"type": "string", "description": "事件摘要"},
            "platform": {"type": "string", "description": "平台名称"},
            "chat_id": {"type": "string", "description": "群聊 ID"},
            "root_message_id": {"type": "string", "description": "根消息 ID"},
            "thread_id": {"type": "string", "description": "Thread ID"},
            "status_card_message_id": {"type": "string", "description": "状态卡片消息 ID"},
            "dedup_key": {"type": "string", "description": "告警去重键"},
            "dedup_key_version": {"type": "string", "description": "去重键版本"},
        },
        "required": ["alert_name", "namespace", "cluster", "summary"],
    },
}

INCIDENT_ADD_EVENT_SCHEMA = {
    "name": "incident_add_event",
    "description": "向事件时间线追加阶段记录。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "事件 ID"},
            "event_type": {"type": "string", "description": "事件类型"},
            "tool_name": {"type": "string", "description": "工具名称"},
            "input_summary": {"type": "string", "description": "输入摘要"},
            "output_summary": {"type": "string", "description": "输出摘要"},
            "metadata": {"type": "object", "description": "附加元数据"},
        },
        "required": ["incident_id", "event_type", "tool_name", "input_summary", "output_summary"],
    },
}

INCIDENT_TIMELINE_SCHEMA = {
    "name": "incident_timeline",
    "description": "读取指定事件的完整时间线。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "事件 ID"},
        },
        "required": ["incident_id"],
    },
}

INCIDENT_LIST_ACTIVE_SCHEMA = {
    "name": "incident_list_active",
    "description": "列出当前活跃事件。",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


async def create_incident(
    alert_name: str,
    namespace: str,
    cluster: str,
    summary: str,
    *,
    platform: str | None = None,
    chat_id: str | None = None,
    root_message_id: str | None = None,
    thread_id: str | None = None,
    status_card_message_id: str | None = None,
    dedup_key: str | None = None,
    dedup_key_version: str | None = None,
) -> str:
    """创建事件。"""
    return await _STORE.create_incident(
        alert_name,
        namespace,
        cluster,
        summary,
        platform=platform,
        chat_id=chat_id,
        root_message_id=root_message_id,
        thread_id=thread_id,
        status_card_message_id=status_card_message_id,
        dedup_key=dedup_key,
        dedup_key_version=dedup_key_version,
    )


async def get_incident(incident_id: str) -> dict[str, Any]:
    """读取事件主记录。"""
    return await _STORE.get_incident(incident_id)


async def add_event(
    incident_id: str,
    event_type: str,
    tool_name: str,
    input_summary: str,
    output_summary: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """追加事件记录。"""
    return await _STORE.add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata)


async def get_timeline(incident_id: str) -> list[dict[str, Any]]:
    """读取事件时间线。"""
    return await _STORE.get_timeline(incident_id)


async def update_status(
    incident_id: str,
    status: str,
    resolved_at: float | None = None,
    closed_at: float | None = None,
) -> None:
    """更新事件状态。"""
    await _STORE.update_status(incident_id, status, resolved_at, closed_at)


async def update_operator(incident_id: str, operator: str) -> None:
    """更新事件负责人。"""
    await _STORE.update_operator(incident_id, operator)


async def list_active() -> list[dict[str, Any]]:
    """列出活跃事件。"""
    return await _STORE.list_active()


async def find_reusable_incident(dedup_key: str, dedup_key_version: str) -> dict[str, Any] | None:
    """查找可复用 incident。"""
    return await _STORE.find_reusable_incident(dedup_key, dedup_key_version)


async def reopen_incident(incident_id: str, reason: str) -> dict[str, Any]:
    """重开已恢复 incident。"""
    return await _STORE.reopen_incident(incident_id, reason)


async def _tool_incident_create(args: dict[str, Any], **_: Any) -> str:
    """工具入口：创建事件。"""
    incident_id = await create_incident(
        args.get("alert_name", ""),
        args.get("namespace", ""),
        args.get("cluster", ""),
        args.get("summary", ""),
        platform=args.get("platform"),
        chat_id=args.get("chat_id"),
        root_message_id=args.get("root_message_id"),
        thread_id=args.get("thread_id"),
        status_card_message_id=args.get("status_card_message_id"),
        dedup_key=args.get("dedup_key"),
        dedup_key_version=args.get("dedup_key_version"),
    )
    return json.dumps({"incident_id": incident_id}, ensure_ascii=False)


async def _tool_incident_add_event(args: dict[str, Any], **_: Any) -> str:
    """工具入口：追加事件。"""
    event_id = await add_event(
        args.get("incident_id", ""),
        args.get("event_type", ""),
        args.get("tool_name", ""),
        args.get("input_summary", ""),
        args.get("output_summary", ""),
        args.get("metadata"),
    )
    return json.dumps({"event_id": event_id}, ensure_ascii=False)


async def _tool_incident_timeline(args: dict[str, Any], **_: Any) -> str:
    """工具入口：读取时间线。"""
    return json.dumps(await get_timeline(args.get("incident_id", "")), ensure_ascii=False)


async def _tool_incident_list_active(args: dict[str, Any], **_: Any) -> str:
    """工具入口：列出活跃事件。"""
    del args
    return json.dumps(await list_active(), ensure_ascii=False)


registry.register(name="incident_create", toolset="sre", schema=INCIDENT_CREATE_SCHEMA, handler=_tool_incident_create, is_async=True)
registry.register(name="incident_add_event", toolset="sre", schema=INCIDENT_ADD_EVENT_SCHEMA, handler=_tool_incident_add_event, is_async=True)
registry.register(name="incident_timeline", toolset="sre", schema=INCIDENT_TIMELINE_SCHEMA, handler=_tool_incident_timeline, is_async=True)
registry.register(name="incident_list_active", toolset="sre", schema=INCIDENT_LIST_ACTIVE_SCHEMA, handler=_tool_incident_list_active, is_async=True)
