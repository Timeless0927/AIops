"""SRE 成本监控守卫。"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml


try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    from tools.registry import registry


T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    incident_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_records(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cost_incident ON cost_records(incident_id);
"""

_DEFAULT_COST_CONFIG = {
    "daily_budget": 50.0,
    "alert_threshold": 0.8,
    "per_incident_budget": 5.0,
    "exceeded_action": "degrade",
}


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    """返回成本数据库路径。"""
    return _project_root() / "data" / "cost_tracking.db"


def _load_cost_config() -> dict[str, Any]:
    """从配置读取成本阈值。"""
    config_path = _project_root() / "config.yaml"
    if not config_path.exists():
        return dict(_DEFAULT_COST_CONFIG)

    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        return dict(_DEFAULT_COST_CONFIG)

    cost_config = config.get("cost")
    if not isinstance(cost_config, dict):
        return dict(_DEFAULT_COST_CONFIG)

    merged = dict(_DEFAULT_COST_CONFIG)
    merged.update(cost_config)
    return merged


class CostGuardDB:
    """基于 SQLite WAL 的成本跟踪存储。"""

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
        self._config = _load_cost_config()

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

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """执行查询并返回单行字典。"""
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def record_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        incident_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """写入一条成本记录。"""
        timestamp = time.time()

        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO cost_records (
                    timestamp, incident_id, model, input_tokens, output_tokens, estimated_cost, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, incident_id, model, input_tokens, output_tokens, estimated_cost, session_id),
            )
            return int(cursor.lastrowid)

        return await asyncio.to_thread(self._execute_write, _write)

    async def get_daily_total(self) -> dict[str, Any]:
        """获取当天累计成本。"""
        start_of_day = int(time.time() // 86400) * 86400

        def _read() -> dict[str, Any]:
            row = self._fetchone(
                """
                SELECT COALESCE(SUM(estimated_cost), 0) AS total_cost, COUNT(*) AS record_count
                FROM cost_records
                WHERE timestamp >= ?
                """,
                (float(start_of_day),),
            ) or {"total_cost": 0.0, "record_count": 0}
            return {"total_cost": float(row["total_cost"] or 0.0), "record_count": int(row["record_count"] or 0)}

        return await asyncio.to_thread(_read)

    async def get_incident_total(self, incident_id: str) -> dict[str, Any]:
        """获取单个事件累计成本。"""

        def _read() -> dict[str, Any]:
            row = self._fetchone(
                """
                SELECT COALESCE(SUM(estimated_cost), 0) AS total_cost, COUNT(*) AS record_count
                FROM cost_records
                WHERE incident_id = ?
                """,
                (incident_id,),
            ) or {"total_cost": 0.0, "record_count": 0}
            return {
                "incident_id": incident_id,
                "total_cost": float(row["total_cost"] or 0.0),
                "record_count": int(row["record_count"] or 0),
            }

        return await asyncio.to_thread(_read)

    async def check_budget(self, incident_id: str | None = None) -> dict[str, Any]:
        """检查当前是否超预算。"""
        daily_total = await self.get_daily_total()
        daily_limit = float(self._config.get("daily_budget", 50.0))
        incident_limit = float(self._config.get("per_incident_budget", 5.0))
        daily_ratio = (daily_total["total_cost"] / daily_limit) if daily_limit else 0.0
        within_budget = daily_total["total_cost"] <= daily_limit
        incident_used: float | None = None

        if incident_id:
            incident_total = await self.get_incident_total(incident_id)
            incident_used = float(incident_total["total_cost"])
            if incident_used > incident_limit:
                within_budget = False

        action = None if within_budget else str(self._config.get("exceeded_action", "degrade"))
        return {
            "within_budget": within_budget,
            "daily_used": float(daily_total["total_cost"]),
            "daily_limit": daily_limit,
            "daily_ratio": daily_ratio,
            "incident_used": incident_used,
            "incident_limit": incident_limit,
            "action": action,
        }


_DB = CostGuardDB()


async def record_cost(model: str, input_tokens: int, output_tokens: int, estimated_cost: float, incident_id: str | None = None, session_id: str | None = None) -> int:
    """模块级成本记录入口。"""
    return await _DB.record_cost(model, input_tokens, output_tokens, estimated_cost, incident_id, session_id)


async def get_daily_total() -> dict[str, Any]:
    """模块级当天成本入口。"""
    return await _DB.get_daily_total()


async def get_incident_total(incident_id: str) -> dict[str, Any]:
    """模块级事件成本入口。"""
    return await _DB.get_incident_total(incident_id)


async def check_budget(incident_id: str | None = None) -> dict[str, Any]:
    """模块级预算检查入口。"""
    return await _DB.check_budget(incident_id)


SRE_COST_RECORD_SCHEMA = {
    "name": "sre_cost_record",
    "description": "记录一次模型调用成本。",
    "parameters": {
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "input_tokens": {"type": "integer"},
            "output_tokens": {"type": "integer"},
            "estimated_cost": {"type": "number"},
            "incident_id": {"type": "string"},
            "session_id": {"type": "string"},
        },
        "required": ["model", "input_tokens", "output_tokens", "estimated_cost"],
    },
}

SRE_COST_CHECK_SCHEMA = {
    "name": "sre_cost_check",
    "description": "检查当前整体或单事件预算状态。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string"},
        },
    },
}


async def _tool_cost_record(args: dict[str, Any], **_: Any) -> str:
    """工具入口：记录成本。"""
    record_id = await record_cost(
        str(args.get("model", "")),
        int(args.get("input_tokens", 0)),
        int(args.get("output_tokens", 0)),
        float(args.get("estimated_cost", 0.0)),
        args.get("incident_id"),
        args.get("session_id"),
    )
    return json.dumps({"record_id": record_id}, ensure_ascii=False)


async def _tool_cost_check(args: dict[str, Any], **_: Any) -> str:
    """工具入口：检查预算。"""
    result = await check_budget(args.get("incident_id"))
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_cost_record", toolset="sre", schema=SRE_COST_RECORD_SCHEMA, handler=_tool_cost_record, is_async=True)
registry.register(name="sre_cost_check", toolset="sre", schema=SRE_COST_CHECK_SCHEMA, handler=_tool_cost_check, is_async=True)
