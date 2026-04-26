"""非阻塞异步审批模块。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import threading
import time
import uuid
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
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    command TEXT NOT NULL,
    context_json TEXT,
    namespace TEXT,
    requester TEXT,
    approver TEXT,
    incident_id TEXT,
    approval_message_id TEXT,
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    executed_at REAL,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_created_at ON approvals(created_at DESC);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    """返回审批数据库路径。"""
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "approvals.db"
    data_dir = _project_root() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "approvals.db"


class ApprovalDB:
    """基于 SQLite WAL 的审批状态存储。"""

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
        self._init_db()

    def _init_db(self) -> None:
        """初始化兼容字段。"""
        for column_sql in (
            "ALTER TABLE approvals ADD COLUMN denial_reason TEXT",
            "ALTER TABLE approvals ADD COLUMN incident_id TEXT",
            "ALTER TABLE approvals ADD COLUMN approval_message_id TEXT",
        ):
            try:
                self._conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass

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

    def request_approval(
        self,
        operation_type: str,
        command: str,
        context: Dict[str, Any] | None,
        namespace: str | None,
        requester: str | None,
        risk_level: str,
        *,
        incident_id: str | None = None,
        approval_message_id: str | None = None,
    ) -> str:
        """创建审批记录并返回审批 ID。"""
        approval_id = str(uuid.uuid4())
        created_at = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO approvals (
                    id, operation_type, command, context_json, namespace,
                    requester, approver, incident_id, approval_message_id,
                    status, risk_level, created_at, decided_at, executed_at, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    operation_type,
                    command,
                    json.dumps(context or {}, ensure_ascii=False),
                    namespace,
                    requester,
                    None,
                    incident_id,
                    approval_message_id,
                    "pending",
                    risk_level,
                    created_at,
                    None,
                    None,
                    None,
                ),
            )
            return approval_id

        return self._execute_write(_write)

    def check_approval(self, approval_id: str) -> Dict[str, Any]:
        """查询审批状态。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return {"found": False, "approval_id": approval_id, "message": "审批记录不存在"}

        result_json = row["result_json"]
        context_json = row["context_json"]
        return {
            "found": True,
            "approval_id": row["id"],
            "operation_type": row["operation_type"],
            "command": row["command"],
            "context": json.loads(context_json) if context_json else {},
            "namespace": row["namespace"],
            "requester": row["requester"],
            "approver": row["approver"],
            "incident_id": row["incident_id"],
            "approval_message_id": row["approval_message_id"],
            "status": row["status"],
            "risk_level": row["risk_level"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "executed_at": row["executed_at"],
            "result": json.loads(result_json) if result_json else None,
        }

    def resolve_approval(self, approval_id: str, decision: str, approver: str, reason: str | None = None) -> Dict[str, Any]:
        """处理审批通过或拒绝。"""
        normalized = decision.strip().lower()
        if normalized not in {"approved", "denied"}:
            return {"ok": False, "message": "decision 仅支持 approved 或 denied", "approval_id": approval_id}

        decided_at = time.time()

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                return {"ok": False, "message": "审批记录不存在", "approval_id": approval_id}
            if row["status"] != "pending":
                return {"ok": False, "message": f"当前状态不允许审批：{row['status']}", "approval_id": approval_id}

            conn.execute(
                "UPDATE approvals SET status = ?, approver = ?, decided_at = ? WHERE id = ?",
                (normalized, approver, decided_at, approval_id),
            )
            if normalized == "denied":
                conn.execute(
                    "UPDATE approvals SET denial_reason = ? WHERE id = ?",
                    (reason, approval_id),
                )
            return {"ok": True, "approval_id": approval_id, "status": normalized, "approver": approver}

        return self._execute_write(_write)

    def execute_approved(self, approval_id: str) -> Dict[str, Any]:
        """将已通过的审批标记为已执行。"""
        executed_at = time.time()

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                return {"ok": False, "message": "审批记录不存在", "approval_id": approval_id}
            if row["status"] != "approved":
                return {"ok": False, "message": f"当前状态不允许执行：{row['status']}", "approval_id": approval_id}

            result = {"executed": True, "approval_id": approval_id}
            conn.execute(
                "UPDATE approvals SET status = ?, executed_at = ?, result_json = ? WHERE id = ?",
                ("executed", executed_at, json.dumps(result, ensure_ascii=False), approval_id),
            )
            return {"ok": True, "approval_id": approval_id, "status": "executed", "executed_at": executed_at}

        return self._execute_write(_write)

    def expire_stale(self, timeout_minutes: int = 30) -> Dict[str, Any]:
        """将超时的 pending 审批标记为 expired。"""
        deadline = time.time() - timeout_minutes * 60

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                (deadline,),
            )
            return {"ok": True, "expired": cursor.rowcount}

        return self._execute_write(_write)


_DB = ApprovalDB()


async def request_approval(
    operation_type: str,
    command: str,
    context: Dict[str, Any] | None,
    namespace: str | None,
    requester: str | None,
    risk_level: str,
    *,
    incident_id: str | None = None,
    approval_message_id: str | None = None,
) -> str:
    """异步创建审批请求。"""
    return await asyncio.to_thread(
        _DB.request_approval,
        operation_type,
        command,
        context,
        namespace,
        requester,
        risk_level,
        incident_id=incident_id,
        approval_message_id=approval_message_id,
    )


async def check_approval(approval_id: str) -> Dict[str, Any]:
    """异步查询审批状态。"""
    return await asyncio.to_thread(_DB.check_approval, approval_id)


async def resolve_approval(approval_id: str, decision: str, approver: str, reason: str | None = None) -> Dict[str, Any]:
    """异步处理审批。"""
    return await asyncio.to_thread(_DB.resolve_approval, approval_id, decision, approver, reason)


async def execute_approved(approval_id: str) -> Dict[str, Any]:
    """异步标记审批已执行。"""
    return await asyncio.to_thread(_DB.execute_approved, approval_id)


async def expire_stale(timeout_minutes: int = 30) -> Dict[str, Any]:
    """异步过期超时审批。"""
    return await asyncio.to_thread(_DB.expire_stale, timeout_minutes)


REQUEST_APPROVAL_SCHEMA = {
    "name": "sre_request_approval",
    "description": "创建异步审批请求并立即返回审批 ID。",
    "parameters": {
        "type": "object",
        "properties": {
            "operation_type": {"type": "string"},
            "command": {"type": "string"},
            "context": {"type": "object"},
            "namespace": {"type": "string"},
            "requester": {"type": "string"},
            "risk_level": {"type": "string"},
            "incident_id": {"type": "string"},
            "approval_message_id": {"type": "string"},
        },
        "required": ["operation_type", "command", "risk_level"],
    },
}

CHECK_APPROVAL_SCHEMA = {
    "name": "sre_check_approval",
    "description": "查询异步审批状态。",
    "parameters": {
        "type": "object",
        "properties": {"approval_id": {"type": "string"}},
        "required": ["approval_id"],
    },
}

SRE_APPROVAL_RESOLVE_SCHEMA = {
    "name": "sre_resolve_approval",
    "description": "对异步审批做通过或拒绝决策。",
    "parameters": {
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["approved", "denied"]},
            "approver": {"type": "string"},
            "reason": {"type": "string", "description": "拒绝原因（仅 denied 时填写）"},
        },
        "required": ["approval_id", "decision", "approver"],
    },
}


async def _request_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：创建审批请求。"""
    approval_id = await request_approval(
        operation_type=args.get("operation_type", ""),
        command=args.get("command", ""),
        context=args.get("context") or {},
        namespace=args.get("namespace"),
        requester=args.get("requester"),
        risk_level=args.get("risk_level", "standard"),
        incident_id=args.get("incident_id"),
        approval_message_id=args.get("approval_message_id"),
    )
    return json.dumps({"ok": True, "approval_id": approval_id}, ensure_ascii=False)


async def _check_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：查询审批状态。"""
    return json.dumps(await check_approval(args.get("approval_id", "")), ensure_ascii=False)


async def _tool_sre_approval_resolve(args: Dict[str, Any], **kwargs: Any) -> str:
    """工具注册入口：审批决策。"""
    return json.dumps(await resolve_approval(
        args.get("approval_id", ""),
        args.get("decision", ""),
        args.get("approver", ""),
        args.get("reason"),
    ), ensure_ascii=False)


registry.register(
    name="sre_request_approval",
    toolset="sre",
    schema=REQUEST_APPROVAL_SCHEMA,
    handler=_request_handler,
    is_async=True,
)

registry.register(
    name="sre_check_approval",
    toolset="sre",
    schema=CHECK_APPROVAL_SCHEMA,
    handler=_check_handler,
    is_async=True,
)

registry.register(
    name="sre_resolve_approval",
    toolset="sre",
    schema=SRE_APPROVAL_RESOLVE_SCHEMA,
    handler=_tool_sre_approval_resolve,
    is_async=True,
)
