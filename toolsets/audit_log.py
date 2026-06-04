"""审计日志存储。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    try:
        from tools.registry import registry
    except ImportError:
        registry = None


T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL,
    what TEXT NOT NULL,
    when_ts REAL NOT NULL,
    cluster TEXT,
    namespace TEXT,
    trigger TEXT,
    tool_level TEXT,
    tool_name TEXT,
    dry_run TEXT,
    result TEXT,
    approval_by TEXT,
    approval_at REAL,
    rollback INTEGER DEFAULT 0,
    snapshot_path TEXT,
    incident_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_when ON audit_log(when_ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_who ON audit_log(who);
CREATE INDEX IF NOT EXISTS idx_audit_cluster_ns ON audit_log(cluster, namespace);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    """返回默认数据库路径。"""
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "audit_log.db"
    return _project_root() / "data" / "audit_log.db"


class AuditLogDB:
    """基于 SQLite WAL 的审计日志存储。"""

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

    async def record_audit(
        self,
        who: str,
        what: str,
        cluster: str | None,
        namespace: str | None,
        trigger: str | None,
        tool_level: str | None,
        tool_name: str | None,
        result: str,
        dry_run: str | None = None,
        approval_by: str | None = None,
        approval_at: float | None = None,
        rollback: bool = False,
        snapshot_path: str | None = None,
        incident_id: str | None = None,
    ) -> int:
        """写入一条审计记录。"""
        when_ts = time.time()

        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO audit_log (
                    who, what, when_ts, cluster, namespace, trigger, tool_level,
                    tool_name, dry_run, result, approval_by, approval_at,
                    rollback, snapshot_path, incident_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    who,
                    what,
                    when_ts,
                    cluster,
                    namespace,
                    trigger,
                    tool_level,
                    tool_name,
                    dry_run,
                    result,
                    approval_by,
                    approval_at,
                    1 if rollback else 0,
                    snapshot_path,
                    incident_id,
                ),
            )
            return int(cursor.lastrowid)

        return await asyncio.to_thread(self._execute_write, _write)

    async def query_audit(
        self,
        time_start: float | None = None,
        time_end: float | None = None,
        who: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按条件查询审计日志。"""

        def _read() -> list[dict[str, Any]]:
            conditions: list[str] = []
            params: list[Any] = []

            if time_start is not None:
                conditions.append("when_ts >= ?")
                params.append(time_start)
            if time_end is not None:
                conditions.append("when_ts <= ?")
                params.append(time_end)
            if who:
                conditions.append("who = ?")
                params.append(who)
            if cluster:
                conditions.append("cluster = ?")
                params.append(cluster)
            if namespace:
                conditions.append("namespace = ?")
                params.append(namespace)

            where_clause = ""
            if conditions:
                where_clause = " WHERE " + " AND ".join(conditions)

            params.append(max(1, int(limit)))
            return self._fetchall(
                f"""
                SELECT id, who, what, when_ts, cluster, namespace, trigger, tool_level,
                       tool_name, dry_run, result, approval_by, approval_at,
                       rollback, snapshot_path, incident_id
                FROM audit_log
                {where_clause}
                ORDER BY when_ts DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            )

        return await asyncio.to_thread(_read)

    async def query_audit_by_incident(self, incident_id: str) -> list[dict[str, Any]]:
        """按 incident_id 查询审计日志。"""

        def _read() -> list[dict[str, Any]]:
            return self._fetchall(
                """
                SELECT id, who, what, when_ts, cluster, namespace, trigger, tool_level,
                       tool_name, dry_run, result, approval_by, approval_at,
                       rollback, snapshot_path, incident_id
                FROM audit_log
                WHERE incident_id = ?
                ORDER BY when_ts DESC, id DESC
                """,
                (incident_id,),
            )

        return await asyncio.to_thread(_read)


_DB = AuditLogDB()


async def record_audit(
    who: str,
    what: str,
    cluster: str | None = None,
    namespace: str | None = None,
    trigger: str | None = "manual",
    tool_level: str | None = None,
    tool_name: str | None = None,
    result: str | None = None,
    dry_run: str | None = None,
    approval_by: str | None = None,
    approval_at: float | None = None,
    rollback: bool = False,
    snapshot_path: str | None = None,
    incident_id: str | None = None,
) -> int:
    """模块级审计写入入口。"""
    return await _DB.record_audit(
        who,
        what,
        cluster,
        namespace,
        trigger,
        tool_level,
        tool_name,
        result,
        dry_run,
        approval_by,
        approval_at,
        rollback,
        snapshot_path,
        incident_id,
    )


async def query_audit(
    time_start: float | None = None,
    time_end: float | None = None,
    who: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """模块级审计查询入口。"""
    return await _DB.query_audit(time_start, time_end, who, cluster, namespace, limit)


async def query_audit_by_incident(incident_id: str) -> list[dict[str, Any]]:
    """模块级 incident 查询入口。"""
    return await _DB.query_audit_by_incident(incident_id)


SRE_AUDIT_RECORD_SCHEMA = {
    "name": "sre_audit_record",
    "description": "写入一条 SRE 审计日志。",
    "parameters": {
        "type": "object",
        "properties": {
            "who": {"type": "string"},
            "what": {"type": "string"},
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "trigger": {"type": "string"},
            "tool_level": {"type": "string"},
            "tool_name": {"type": "string"},
            "result": {"type": "string"},
            "dry_run": {"type": "string"},
            "approval_by": {"type": "string"},
            "approval_at": {"type": "number"},
            "rollback": {"type": "boolean"},
            "snapshot_path": {"type": "string"},
            "incident_id": {"type": "string"},
        },
        "required": ["who", "what", "result"],
    },
}

SRE_AUDIT_QUERY_SCHEMA = {
    "name": "sre_audit_query",
    "description": "查询 SRE 审计日志。",
    "parameters": {
        "type": "object",
        "properties": {
            "time_start": {"type": "number"},
            "time_end": {"type": "number"},
            "who": {"type": "string"},
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
}


async def _tool_sre_audit_record(args: dict[str, Any], **_: Any) -> str:
    """工具入口：写入审计。"""
    audit_id = await record_audit(
        who=args.get("who", ""),
        what=args.get("what", ""),
        cluster=args.get("cluster"),
        namespace=args.get("namespace"),
        trigger=args.get("trigger"),
        tool_level=args.get("tool_level"),
        tool_name=args.get("tool_name"),
        result=args.get("result", ""),
        dry_run=args.get("dry_run"),
        approval_by=args.get("approval_by"),
        approval_at=args.get("approval_at"),
        rollback=bool(args.get("rollback", False)),
        snapshot_path=args.get("snapshot_path"),
        incident_id=args.get("incident_id"),
    )
    return json.dumps({"audit_id": audit_id}, ensure_ascii=False)


async def _tool_sre_audit_query(args: dict[str, Any], **_: Any) -> str:
    """工具入口：查询审计。"""
    return json.dumps(await query_audit(
        time_start=args.get("time_start"),
        time_end=args.get("time_end"),
        who=args.get("who"),
        cluster=args.get("cluster"),
        namespace=args.get("namespace"),
        limit=int(args.get("limit", 100)),
    ), ensure_ascii=False)


if registry is not None:  # pragma: no cover - 注册行为由集成环境覆盖
    registry.register(name="sre_audit_record", toolset="sre", schema=SRE_AUDIT_RECORD_SCHEMA, handler=_tool_sre_audit_record, is_async=True)
    registry.register(name="sre_audit_query", toolset="sre", schema=SRE_AUDIT_QUERY_SCHEMA, handler=_tool_sre_audit_query, is_async=True)
