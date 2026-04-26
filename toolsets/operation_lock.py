"""并发操作锁模块。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

import sys


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


_ensure_registry_import()

from tools.registry import registry  # noqa: E402


T = TypeVar("T")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operation_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_key TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operation_locks_expires_at ON operation_locks(expires_at);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    """返回操作锁数据库路径。"""
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "operation_locks.db"
    data_dir = _project_root() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "operation_locks.db"


class OperationLockDB:
    """基于 SQLite WAL 的资源并发锁。"""

    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150
    _CHECKPOINT_EVERY_N_WRITES = 50

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
        self._conn.executescript(SCHEMA_SQL)

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """使用 BEGIN IMMEDIATE 和抖动重试执行写事务。"""
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
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
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if ("locked" in message or "busy" in message) and attempt < self._WRITE_MAX_RETRIES - 1:
                    last_err = exc
                    time.sleep(random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S))
                    continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        """尽力执行 WAL checkpoint。"""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except Exception:
            pass

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    def cleanup_expired(self) -> Dict[str, Any]:
        """清理已过期的锁。"""
        now = time.time()

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cursor = conn.execute("DELETE FROM operation_locks WHERE expires_at <= ?", (now,))
            return {"ok": True, "deleted": cursor.rowcount}

        return self._execute_write(_write)

    def acquire_lock(self, resource_key: str, session_id: str, ttl_seconds: int = 300) -> bool:
        """尝试获取资源锁。"""
        now = time.time()
        expires_at = now + ttl_seconds

        def _write(conn: sqlite3.Connection) -> bool:
            conn.execute("DELETE FROM operation_locks WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT session_id, expires_at FROM operation_locks WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                return False
            conn.execute(
                """
                INSERT INTO operation_locks (resource_key, session_id, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (resource_key, session_id, now, expires_at),
            )
            return True

        return self._execute_write(_write)

    def release_lock(self, resource_key: str, session_id: str) -> bool:
        """按资源和会话双因子释放锁。"""

        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM operation_locks WHERE resource_key = ? AND session_id = ?",
                (resource_key, session_id),
            )
            return cursor.rowcount > 0

        return self._execute_write(_write)

    def is_locked(self, resource_key: str) -> bool:
        """判断资源当前是否被有效锁持有。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM operation_locks WHERE resource_key = ? AND expires_at > ?",
                (resource_key, now),
            ).fetchone()
        return row is not None


_DB = OperationLockDB()


async def acquire_lock(resource_key: str, session_id: str, ttl_seconds: int = 300) -> bool:
    """异步获取资源锁。"""
    return await asyncio.to_thread(_DB.acquire_lock, resource_key, session_id, ttl_seconds)


async def release_lock(resource_key: str, session_id: str) -> bool:
    """异步释放资源锁。"""
    return await asyncio.to_thread(_DB.release_lock, resource_key, session_id)


async def is_locked(resource_key: str) -> bool:
    """异步检查资源锁状态。"""
    return await asyncio.to_thread(_DB.is_locked, resource_key)


async def cleanup_expired() -> Dict[str, Any]:
    """异步清理过期锁。"""
    return await asyncio.to_thread(_DB.cleanup_expired)


ACQUIRE_LOCK_SCHEMA = {
    "name": "sre_acquire_lock",
    "description": "为资源获取并发操作锁。",
    "parameters": {
        "type": "object",
        "properties": {
            "resource_key": {"type": "string"},
            "session_id": {"type": "string"},
            "ttl_seconds": {"type": "integer"},
        },
        "required": ["resource_key", "session_id"],
    },
}

RELEASE_LOCK_SCHEMA = {
    "name": "sre_release_lock",
    "description": "释放资源并发锁。",
    "parameters": {
        "type": "object",
        "properties": {
            "resource_key": {"type": "string"},
            "session_id": {"type": "string"},
        },
        "required": ["resource_key", "session_id"],
    },
}

CHECK_LOCK_SCHEMA = {
    "name": "sre_check_lock",
    "description": "检查资源当前是否被锁定。",
    "parameters": {
        "type": "object",
        "properties": {"resource_key": {"type": "string"}},
        "required": ["resource_key"],
    },
}


async def _acquire_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：获取锁。"""
    acquired = await acquire_lock(
        args.get("resource_key", ""),
        args.get("session_id", ""),
        int(args.get("ttl_seconds", 300)),
    )
    return json.dumps({"ok": acquired, "acquired": acquired}, ensure_ascii=False)


async def _release_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：释放锁。"""
    released = await release_lock(args.get("resource_key", ""), args.get("session_id", ""))
    return json.dumps({"ok": released, "released": released}, ensure_ascii=False)


async def _check_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：检查锁。"""
    locked = await is_locked(args.get("resource_key", ""))
    return json.dumps({"ok": True, "locked": locked}, ensure_ascii=False)


registry.register(
    name="sre_acquire_lock",
    toolset="sre",
    schema=ACQUIRE_LOCK_SCHEMA,
    handler=_acquire_handler,
    is_async=True,
)

registry.register(
    name="sre_release_lock",
    toolset="sre",
    schema=RELEASE_LOCK_SCHEMA,
    handler=_release_handler,
    is_async=True,
)

registry.register(
    name="sre_check_lock",
    toolset="sre",
    schema=CHECK_LOCK_SCHEMA,
    handler=_check_handler,
    is_async=True,
)
