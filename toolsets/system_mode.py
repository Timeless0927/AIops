"""系统运行模式存储。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_VALID_MODES = {"normal", "degraded", "read_only"}
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_mode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    reason TEXT,
    updated_at REAL NOT NULL
);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    """返回默认数据库路径。"""
    return _project_root() / "data" / "system_mode.db"


class SystemModeDB:
    """基于 SQLite 的系统模式存储。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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
        """关闭数据库连接。"""
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def get_mode(self) -> dict[str, Any]:
        """读取当前系统模式；未初始化时返回默认值。"""
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(
                "SELECT mode, reason, updated_at FROM system_mode WHERE id = 1"
            ).fetchone()

        if row is None:
            return {"mode": "normal", "reason": None, "updated_at": 0.0}
        return dict(row)

    def set_mode(self, mode: str, reason: str | None = None) -> dict[str, Any]:
        """更新当前系统模式。"""
        if mode not in _VALID_MODES:
            raise ValueError(f"不支持的 system_mode: {mode}")

        updated_at = time.time()
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO system_mode (id, mode, reason, updated_at)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        mode = excluded.mode,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (mode, reason, updated_at),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        return {"mode": mode, "reason": reason, "updated_at": updated_at}


_DB = SystemModeDB()


async def get_system_mode() -> dict[str, Any]:
    """异步读取系统运行模式。"""
    return await asyncio.to_thread(_DB.get_mode)


async def set_system_mode(mode: str, reason: str | None = None) -> dict[str, Any]:
    """异步更新系统运行模式。"""
    return await asyncio.to_thread(_DB.set_mode, mode, reason)
