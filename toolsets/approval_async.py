"""非阻塞异步审批模块。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.machinery
import importlib.util
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

import yaml


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    project_root = Path(__file__).resolve().parents[1]
    hermes_root = project_root / "hermes-agent"
    hermes_root_text = str(hermes_root)

    project_root_index: int | None = None
    for index, path_entry in enumerate(sys.path):
        try:
            resolved_entry = Path(path_entry or os.getcwd()).resolve()
        except OSError:
            continue
        if resolved_entry == project_root:
            project_root_index = index
            break

    sys.path[:] = [
        path_entry
        for path_entry in sys.path
        if Path(path_entry or os.getcwd()).resolve() != hermes_root
    ]
    if project_root_index is None:
        sys.path.append(hermes_root_text)
    else:
        sys.path.insert(project_root_index + 1, hermes_root_text)


def _restore_project_toolsets_package() -> None:
    """恢复本仓库 toolsets namespace package，避免 Hermes 同名模块污染。"""
    project_root = Path(__file__).resolve().parents[1]
    hermes_root = project_root / "hermes-agent"
    hermes_toolsets = project_root / "hermes-agent" / "toolsets.py"
    cached_toolsets = sys.modules.get("toolsets")

    sys.path[:] = [
        path_entry
        for path_entry in sys.path
        if Path(path_entry or os.getcwd()).resolve() != hermes_root
    ]

    if cached_toolsets is not None:
        cached_file = getattr(cached_toolsets, "__file__", None)
        cached_path = getattr(cached_toolsets, "__path__", None)
        try:
            cached_is_hermes_module = (
                cached_file is not None
                and Path(cached_file).resolve() == hermes_toolsets
                and cached_path is None
            )
        except OSError:
            cached_is_hermes_module = False
        if not cached_is_hermes_module:
            return
        sys.modules.pop("toolsets", None)

    package_spec = importlib.machinery.PathFinder.find_spec("toolsets", [str(project_root)])
    if package_spec is None or package_spec.submodule_search_locations is None:
        return
    sys.modules["toolsets"] = importlib.util.module_from_spec(package_spec)


_ensure_registry_import()

from tools.registry import registry  # noqa: E402

_restore_project_toolsets_package()

from runtime.feishu_approval_overlay import build_approval_card_payload  # noqa: E402


T = TypeVar("T")

_EXTERNAL_FINAL_STATUSES = {
    "approved",
    "executed",
    "failed",
    "denied",
    "canceled",
    "expired",
    "approval_create_failed",
}
_EXTERNAL_STATUS_MAP = {
    "APPROVED": "approved",
    "REJECTED": "denied",
    "CANCELED": "canceled",
}


def _load_tool_module(module_basename: str, alias: str):
    """按文件路径加载 toolsets 模块，避免包导入冲突。"""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = Path(__file__).resolve().parent / f"{module_basename}.py"
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _load_hook_module(module_basename: str, alias: str):
    """按文件路径加载 hooks 模块，避免包导入冲突。"""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = Path(__file__).resolve().parents[1] / "hooks" / f"{module_basename}.py"
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


incident_store = _load_tool_module("incident_store", "aiops_approval_async_incident_store")
message_delivery = _load_tool_module("message_delivery", "aiops_approval_async_message_delivery")
feishu_conversation = _load_hook_module("feishu_conversation", "aiops_approval_async_feishu_conversation")

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
    result_json TEXT,
    external_provider TEXT,
    external_uuid TEXT,
    external_approval_code TEXT,
    external_instance_code TEXT,
    external_status TEXT,
    external_url TEXT,
    external_created_at REAL,
    external_updated_at REAL,
    external_last_error TEXT,
    external_poll_attempts INTEGER DEFAULT 0,
    external_next_poll_at REAL
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_created_at ON approvals(created_at DESC);

CREATE TABLE IF NOT EXISTS approval_executions (
    id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    incident_id TEXT,
    action_signature TEXT NOT NULL,
    action_schema_version TEXT NOT NULL,
    action_type TEXT NOT NULL,
    cluster TEXT,
    namespace TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run_result_json TEXT,
    lock_key TEXT,
    audit_id INTEGER,
    health_result_json TEXT,
    rollback_result_json TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    FOREIGN KEY (approval_id) REFERENCES approvals(id)
);

CREATE INDEX IF NOT EXISTS idx_approval_executions_status
ON approval_executions(status, updated_at);
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


def _runtime_config_candidates() -> list[Path]:
    """返回运行时配置候选路径，按优先级排序。"""
    candidates: list[Path] = []

    hermes_config = os.getenv("HERMES_CONFIG")
    if hermes_config:
        candidates.append(Path(hermes_config).expanduser())

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")

    return candidates


def _load_config_sync() -> Dict[str, Any]:
    """同步读取运行时配置。"""
    for path in _runtime_config_candidates():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    return {}


async def _load_config() -> Dict[str, Any]:
    """异步读取项目配置。"""
    return await asyncio.to_thread(_load_config_sync)


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
            "ALTER TABLE approvals ADD COLUMN external_provider TEXT",
            "ALTER TABLE approvals ADD COLUMN external_uuid TEXT",
            "ALTER TABLE approvals ADD COLUMN external_approval_code TEXT",
            "ALTER TABLE approvals ADD COLUMN external_instance_code TEXT",
            "ALTER TABLE approvals ADD COLUMN external_status TEXT",
            "ALTER TABLE approvals ADD COLUMN external_url TEXT",
            "ALTER TABLE approvals ADD COLUMN external_created_at REAL",
            "ALTER TABLE approvals ADD COLUMN external_updated_at REAL",
            "ALTER TABLE approvals ADD COLUMN external_last_error TEXT",
            "ALTER TABLE approvals ADD COLUMN external_poll_attempts INTEGER DEFAULT 0",
            "ALTER TABLE approvals ADD COLUMN external_next_poll_at REAL",
        ):
            try:
                self._conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_external_uuid ON approvals(external_uuid)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approvals_external_instance_code ON approvals(external_instance_code)"
        )

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

    def record_external_approval_created(
        self,
        approval_id: str,
        *,
        provider: str,
        external_uuid: str | None = None,
        external_approval_code: str | None = None,
        external_instance_code: str | None = None,
        external_status: str | None = None,
        external_url: str | None = None,
    ) -> Dict[str, Any]:
        """记录外部审批实例创建成功。"""
        now = time.time()
        normalized_status = str(external_status or "PENDING").strip() or "PENDING"

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                return {"ok": False, "message": "审批记录不存在", "approval_id": approval_id}
            current_status = str(row["status"])
            if current_status in _EXTERNAL_FINAL_STATUSES or current_status == "approved":
                return {"ok": False, "approval_id": approval_id, "status": current_status}
            conn.execute(
                """
                UPDATE approvals
                SET status = 'external_pending',
                    external_provider = ?,
                    external_uuid = ?,
                    external_approval_code = ?,
                    external_instance_code = ?,
                    external_status = ?,
                    external_url = ?,
                    external_created_at = COALESCE(external_created_at, ?),
                    external_updated_at = ?,
                    external_last_error = NULL,
                    external_poll_attempts = 0,
                    external_next_poll_at = NULL
                WHERE id = ?
                """,
                (
                    provider,
                    external_uuid or approval_id,
                    external_approval_code,
                    external_instance_code,
                    normalized_status,
                    external_url,
                    now,
                    now,
                    approval_id,
                ),
            )
            return {
                "ok": True,
                "approval_id": approval_id,
                "status": "external_pending",
                "external_provider": provider,
                "external_uuid": external_uuid or approval_id,
                "external_approval_code": external_approval_code,
                "external_instance_code": external_instance_code,
                "external_status": normalized_status,
                "external_url": external_url,
            }

        return self._execute_write(_write)

    def record_external_approval_create_failed(
        self,
        approval_id: str,
        *,
        provider: str,
        error_type: str,
        message: str,
    ) -> Dict[str, Any]:
        """记录外部审批实例创建失败。"""
        now = time.time()
        last_error = {"error_type": error_type, "message": message}

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                return {"ok": False, "message": "审批记录不存在", "approval_id": approval_id}
            current_status = str(row["status"])
            if current_status in _EXTERNAL_FINAL_STATUSES or current_status == "approved":
                return {"ok": False, "approval_id": approval_id, "status": current_status}
            conn.execute(
                """
                UPDATE approvals
                SET status = 'approval_create_failed',
                    external_provider = ?,
                    external_updated_at = ?,
                    external_last_error = ?
                WHERE id = ?
                """,
                (provider, now, json.dumps(last_error, ensure_ascii=False), approval_id),
            )
            return {
                "ok": True,
                "approval_id": approval_id,
                "status": "approval_create_failed",
                "external_provider": provider,
                "external_last_error": last_error,
            }

        return self._execute_write(_write)

    def update_approval_message_id(self, approval_id: str, message_id: str) -> Dict[str, Any]:
        """回写审批对应的飞书消息 ID。"""
        normalized_approval_id = str(approval_id).strip()
        normalized_message_id = str(message_id).strip()
        if not normalized_approval_id:
            return {"ok": False, "message": "approval_id 不能为空", "approval_id": ""}
        if not normalized_message_id:
            return {"ok": False, "message": "approval_message_id 不能为空", "approval_id": normalized_approval_id}

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cursor = conn.execute(
                "UPDATE approvals SET approval_message_id = ? WHERE id = ?",
                (normalized_message_id, normalized_approval_id),
            )
            if cursor.rowcount == 0:
                return {"ok": False, "message": "审批记录不存在", "approval_id": normalized_approval_id}
            return {
                "ok": True,
                "approval_id": normalized_approval_id,
                "approval_message_id": normalized_message_id,
            }

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
        external_last_error = row["external_last_error"]
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
            "external_provider": row["external_provider"],
            "external_uuid": row["external_uuid"],
            "external_approval_code": row["external_approval_code"],
            "external_instance_code": row["external_instance_code"],
            "external_status": row["external_status"],
            "external_url": row["external_url"],
            "external_created_at": row["external_created_at"],
            "external_updated_at": row["external_updated_at"],
            "external_last_error": json.loads(external_last_error) if external_last_error else None,
            "external_poll_attempts": row["external_poll_attempts"] or 0,
            "external_next_poll_at": row["external_next_poll_at"],
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

    def resolve_external_approval(
        self,
        *,
        external_uuid: str | None,
        external_instance_code: str | None,
        external_status: str,
        source: str,
        raw_event: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """幂等同步飞书原生审批状态。"""
        normalized_status = str(external_status or "").strip().upper()
        target_status = _EXTERNAL_STATUS_MAP.get(normalized_status)
        if not target_status:
            return {"ok": False, "reason": "unsupported_status", "status": "ignored"}

        uuid_value = str(external_uuid or "").strip()
        instance_code = str(external_instance_code or "").strip()
        if not uuid_value and not instance_code:
            return {"ok": False, "reason": "missing_identifier", "status": "ignored"}

        now = time.time()
        raw_event = raw_event if isinstance(raw_event, dict) else {}
        operator = raw_event.get("operator") if isinstance(raw_event.get("operator"), dict) else {}
        approver = str(operator.get("open_id") or operator.get("user_id") or source or "").strip() or source
        is_polling_sync = str(source or "").strip().lower() == "feishu_polling"

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            params: list[Any] = []
            predicates: list[str] = []
            if uuid_value:
                predicates.append("external_uuid = ?")
                params.append(uuid_value)
            if instance_code:
                predicates.append("external_instance_code = ?")
                params.append(instance_code)
            row = conn.execute(
                f"""
                SELECT id, status, external_provider, external_uuid, external_instance_code
                FROM approvals
                WHERE {' OR '.join(predicates)}
                ORDER BY created_at DESC LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found", "status": "ignored"}

            approval_id = row["id"]
            current_status = str(row["status"])
            provider = str(row["external_provider"] or "").strip().lower()
            bound_uuid = str(row["external_uuid"] or "").strip()
            bound_instance_code = str(row["external_instance_code"] or "").strip()
            has_matching_external_binding = provider == "feishu" and (
                (uuid_value and bound_uuid == uuid_value)
                or (instance_code and bound_instance_code == instance_code)
            )
            if not has_matching_external_binding:
                return {
                    "ok": False,
                    "approval_id": approval_id,
                    "reason": "missing_external_binding",
                    "status": "ignored",
                }

            if current_status == target_status:
                conn.execute(
                    """
                    UPDATE approvals
                    SET external_status = ?,
                        external_updated_at = ?
                    WHERE id = ?
                    """,
                    (normalized_status, now, approval_id),
                )
                return {"ok": True, "approval_id": approval_id, "status": target_status}

            if current_status in _EXTERNAL_FINAL_STATUSES:
                return {"ok": False, "approval_id": approval_id, "status": current_status}

            if current_status != "external_pending":
                return {
                    "ok": False,
                    "approval_id": approval_id,
                    "reason": "not_external_pending",
                    "status": "ignored",
                }

            conn.execute(
                """
                UPDATE approvals
                SET status = ?,
                    external_status = ?,
                    external_updated_at = ?,
                    external_last_error = NULL,
                    external_next_poll_at = NULL,
                    external_poll_attempts = COALESCE(external_poll_attempts, 0) + ?,
                    decided_at = COALESCE(decided_at, ?),
                    approver = COALESCE(approver, ?)
                WHERE id = ?
                """,
                (target_status, normalized_status, now, 1 if is_polling_sync else 0, now, approver, approval_id),
            )
            return {"ok": True, "approval_id": approval_id, "status": target_status}

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

    def find_pending_approval(self, incident_id: str, action_signature: str) -> Dict[str, Any] | None:
        """按 incident 和 action signature 查找未决审批。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM approvals
                WHERE incident_id = ? AND status IN ('pending', 'external_pending')
                ORDER BY created_at DESC
                """,
                (incident_id,),
            ).fetchall()
        for row in rows:
            context_json = row["context_json"]
            context = json.loads(context_json) if context_json else {}
            if context.get("action_signature") == action_signature:
                return self.check_approval(row["id"])
        return None

    def list_pending_without_message_before(self, deadline: float) -> list[dict[str, Any]]:
        """查找指定时间点之前仍未回写 approval_message_id 的 pending 审批。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, incident_id, created_at
                FROM approvals
                WHERE status = 'pending'
                  AND (approval_message_id IS NULL OR TRIM(approval_message_id) = '')
                  AND created_at < ?
                ORDER BY created_at ASC
                """,
                (deadline,),
            ).fetchall()
        return [
            {
                "approval_id": row["id"],
                "incident_id": row["incident_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_external_pending_approvals(
        self,
        *,
        limit: int = 50,
        now: float | None = None,
        stale_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        """列出需要 polling 补偿的外部待审批记录。"""
        safe_limit = max(1, min(int(limit or 50), 500))
        current_time = time.time() if now is None else float(now)
        try:
            stale_window = max(0, int(stale_seconds or 0))
        except (TypeError, ValueError):
            stale_window = 0
        stale_cutoff = current_time - stale_window
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, external_uuid, external_instance_code, external_status,
                       external_poll_attempts, external_next_poll_at,
                       external_updated_at, external_created_at, created_at
                FROM approvals
                WHERE status = 'external_pending'
                  AND external_provider = 'feishu'
                  AND external_instance_code IS NOT NULL
                  AND TRIM(external_instance_code) != ''
                  AND (external_next_poll_at IS NULL OR external_next_poll_at <= ?)
                  AND (? <= 0 OR COALESCE(external_updated_at, external_created_at, created_at) <= ?)
                ORDER BY COALESCE(external_updated_at, external_created_at, created_at) ASC
                LIMIT ?
                """,
                (current_time, stale_window, stale_cutoff, safe_limit),
            ).fetchall()
        return [
            {
                "approval_id": row["id"],
                "external_uuid": row["external_uuid"],
                "external_instance_code": row["external_instance_code"],
                "external_status": row["external_status"],
                "external_poll_attempts": row["external_poll_attempts"] or 0,
                "external_next_poll_at": row["external_next_poll_at"],
                "external_updated_at": row["external_updated_at"],
                "external_created_at": row["external_created_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_external_poll_failure(
        self,
        approval_id: str,
        *,
        error_type: str,
        message: str,
        backoff_seconds: int,
    ) -> Dict[str, Any]:
        """记录 polling 查询失败并设置退避。"""
        now = time.time()
        next_poll_at = now + max(1, int(backoff_seconds or 1))
        last_error = {"error_type": error_type, "message": message}

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET external_poll_attempts = COALESCE(external_poll_attempts, 0) + 1,
                    external_next_poll_at = ?,
                    external_updated_at = ?,
                    external_last_error = ?
                WHERE id = ? AND status = 'external_pending'
                """,
                (next_poll_at, now, json.dumps(last_error, ensure_ascii=False), approval_id),
            )
            return {
                "ok": cursor.rowcount > 0,
                "approval_id": approval_id,
                "external_next_poll_at": next_poll_at,
                "external_last_error": last_error,
            }

        return self._execute_write(_write)

    def record_external_poll_pending(
        self,
        approval_id: str,
        *,
        external_status: str = "PENDING",
        interval_seconds: int,
        now: float | None = None,
    ) -> Dict[str, Any]:
        """记录仍为 PENDING 的外部审批，并写入下一次 polling 时间。"""
        current_time = time.time() if now is None else float(now)
        next_poll_at = current_time + max(1, int(interval_seconds or 1))
        normalized_status = str(external_status or "PENDING").strip().upper() or "PENDING"

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET external_status = ?,
                    external_poll_attempts = COALESCE(external_poll_attempts, 0) + 1,
                    external_next_poll_at = ?,
                    external_updated_at = ?,
                    external_last_error = NULL
                WHERE id = ? AND status = 'external_pending'
                """,
                (normalized_status, next_poll_at, current_time, approval_id),
            )
            return {
                "ok": cursor.rowcount > 0,
                "approval_id": approval_id,
                "external_status": normalized_status,
                "external_next_poll_at": next_poll_at,
            }

        return self._execute_write(_write)

    def expire_stale(self, timeout_minutes: int = 30) -> Dict[str, Any]:
        """将超时的 pending 审批标记为 expired。"""
        deadline = time.time() - timeout_minutes * 60

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            rows = conn.execute(
                """
                SELECT id, incident_id FROM approvals
                WHERE status = 'pending' AND created_at < ?
                """,
                (deadline,),
            ).fetchall()
            cursor = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                (deadline,),
            )
            approvals = [{"approval_id": row["id"], "incident_id": row["incident_id"]} for row in rows]
            return {"ok": True, "expired": cursor.rowcount, "approvals": approvals}

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


async def request_external_approval(
    operation_type: str,
    command: str,
    context: Dict[str, Any] | None,
    namespace: str | None,
    requester: str | None,
    risk_level: str,
    *,
    incident_id: str | None = None,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """创建本地审批记录，供外部审批实例绑定。"""
    del config
    approval_id = await request_approval(
        operation_type,
        command,
        context,
        namespace,
        requester,
        risk_level,
        incident_id=incident_id,
    )
    return {"ok": True, "approval_id": approval_id, "status": "pending"}


async def check_approval(approval_id: str) -> Dict[str, Any]:
    """异步查询审批状态。"""
    return await asyncio.to_thread(_DB.check_approval, approval_id)


async def record_external_approval_created(
    approval_id: str,
    *,
    provider: str,
    external_uuid: str | None = None,
    external_approval_code: str | None = None,
    external_instance_code: str | None = None,
    external_status: str | None = None,
    external_url: str | None = None,
) -> Dict[str, Any]:
    """异步记录外部审批创建成功。"""
    return await asyncio.to_thread(
        _DB.record_external_approval_created,
        approval_id,
        provider=provider,
        external_uuid=external_uuid,
        external_approval_code=external_approval_code,
        external_instance_code=external_instance_code,
        external_status=external_status,
        external_url=external_url,
    )


async def record_external_approval_create_failed(
    approval_id: str,
    *,
    provider: str,
    error_type: str,
    message: str,
) -> Dict[str, Any]:
    """异步记录外部审批创建失败。"""
    return await asyncio.to_thread(
        _DB.record_external_approval_create_failed,
        approval_id,
        provider=provider,
        error_type=error_type,
        message=message,
    )


async def resolve_approval(approval_id: str, decision: str, approver: str, reason: str | None = None) -> Dict[str, Any]:
    """异步处理审批。"""
    return await asyncio.to_thread(_DB.resolve_approval, approval_id, decision, approver, reason)


async def resolve_external_approval(
    *,
    external_uuid: str | None,
    external_instance_code: str | None,
    external_status: str,
    source: str,
    raw_event: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """异步同步外部审批状态。"""
    return await asyncio.to_thread(
        _DB.resolve_external_approval,
        external_uuid=external_uuid,
        external_instance_code=external_instance_code,
        external_status=external_status,
        source=source,
        raw_event=raw_event,
    )


async def execute_approved(approval_id: str) -> Dict[str, Any]:
    """异步标记审批已执行。"""
    return await asyncio.to_thread(_DB.execute_approved, approval_id)


async def find_pending_approval(incident_id: str, action_signature: str) -> Dict[str, Any] | None:
    """异步查找 incident 下相同动作的未决审批。"""
    return await asyncio.to_thread(_DB.find_pending_approval, incident_id, action_signature)


async def expire_stale(timeout_minutes: int = 30) -> Dict[str, Any]:
    """异步过期超时审批。"""
    return await asyncio.to_thread(_DB.expire_stale, timeout_minutes)


async def update_approval_message_id(approval_id: str, message_id: str) -> Dict[str, Any]:
    """异步回写审批对应的飞书消息 ID。"""
    return await asyncio.to_thread(_DB.update_approval_message_id, approval_id, message_id)


async def list_pending_without_message_before(deadline: float) -> list[dict[str, Any]]:
    """异步查找指定时间点之前仍未回写 approval_message_id 的 pending 审批。"""
    return await asyncio.to_thread(_DB.list_pending_without_message_before, deadline)


async def list_external_pending_approvals(
    *,
    limit: int = 50,
    now: float | None = None,
    stale_seconds: int = 0,
) -> list[dict[str, Any]]:
    """异步列出需要 polling 补偿的外部待审批记录。"""
    return await asyncio.to_thread(_DB.list_external_pending_approvals, limit=limit, now=now, stale_seconds=stale_seconds)


async def record_external_poll_failure(
    approval_id: str,
    *,
    error_type: str,
    message: str,
    backoff_seconds: int,
) -> Dict[str, Any]:
    """异步记录 external_pending polling 失败。"""
    return await asyncio.to_thread(
        _DB.record_external_poll_failure,
        approval_id,
        error_type=error_type,
        message=message,
        backoff_seconds=backoff_seconds,
    )


async def record_external_poll_pending(
    approval_id: str,
    *,
    external_status: str = "PENDING",
    interval_seconds: int,
    now: float | None = None,
) -> Dict[str, Any]:
    """异步记录 PENDING polling 节流。"""
    return await asyncio.to_thread(
        _DB.record_external_poll_pending,
        approval_id,
        external_status=external_status,
        interval_seconds=interval_seconds,
        now=now,
    )


def _stable_payload_hash(payload: dict[str, Any]) -> str:
    """为投递 payload 生成稳定哈希。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def publish_or_queue_approval_card(
    approval_id: str,
    *,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """尝试发送审批卡片；缺少飞书绑定时返回 pending_retry。"""
    approval = await check_approval(approval_id)
    if not approval.get("found"):
        return {
            "ok": False,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "failed",
            "message": approval.get("message") or "审批记录不存在",
        }

    existing_message_id = str(approval.get("approval_message_id") or "").strip()
    if existing_message_id:
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": existing_message_id,
            "delivery_status": "sent",
        }

    sent_delivery = await message_delivery.find_sent_delivery_for_approval(
        approval_id=approval_id,
        target_type="approval_card",
    )
    if sent_delivery is not None:
        target_message_id = str(sent_delivery.get("target_message_id") or "").strip()
        if target_message_id:
            await update_approval_message_id(approval_id, target_message_id)
            return {
                "ok": True,
                "approval_id": approval_id,
                "approval_message_id": target_message_id,
                "delivery_status": "sent",
                "delivery_id": sent_delivery.get("id"),
            }

    incident_id = str(approval.get("incident_id") or "").strip()
    if not incident_id:
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "message": "审批记录尚未关联 incident",
        }

    try:
        incident = await incident_store.get_incident(incident_id)
    except Exception as exc:
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "message": f"incident 不可用: {exc}",
        }

    if not isinstance(incident, dict):
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "message": "incident 数据格式不正确",
        }

    chat_id = str(incident.get("chat_id") or "").strip()
    if not chat_id:
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "message": "incident 飞书绑定未就绪",
        }

    approval_payload = build_approval_card_payload(approval)
    payload_hash = _stable_payload_hash(approval_payload)
    thread_id = str(incident.get("thread_id") or incident.get("root_message_id") or incident.get("status_card_message_id") or "").strip()
    delivery_id = await message_delivery.upsert_delivery(
        incident_id=incident_id,
        target_type="approval_card",
        platform="feishu",
        chat_id=chat_id,
        thread_id=thread_id or None,
        approval_id=approval_id,
        payload_hash=payload_hash,
    )

    effective_config = config if config is not None else await _load_config()
    try:
        response = await feishu_conversation.publish_approval_card(approval, incident, effective_config)
    except Exception as exc:
        await message_delivery.mark_failed(delivery_id, str(exc))
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": str(exc),
        }

    message_id = str(response.get("message_id") or "").strip()
    if not message_id:
        await message_delivery.mark_failed(delivery_id, "飞书审批卡片未返回 message_id")
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": "飞书审批卡片未返回 message_id",
        }

    await message_delivery.mark_sent(delivery_id, message_id)
    await update_approval_message_id(approval_id, message_id)
    return {
        "ok": True,
        "approval_id": approval_id,
        "approval_message_id": message_id,
        "delivery_status": "sent",
        "delivery_id": delivery_id,
        "root_message_id": response.get("root_message_id"),
        "thread_id": response.get("thread_id"),
    }


async def request_approval_with_card(
    operation_type: str,
    command: str,
    context: Dict[str, Any] | None,
    namespace: str | None,
    requester: str | None,
    risk_level: str,
    *,
    incident_id: str | None = None,
    approval_message_id: str | None = None,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """创建审批并尽力投递对应的飞书审批卡片。"""
    approval_id = await request_approval(
        operation_type,
        command,
        context,
        namespace,
        requester,
        risk_level,
        incident_id=incident_id,
        approval_message_id=None,
    )
    delivery = await publish_or_queue_approval_card(approval_id, config=config)
    result = {
        "ok": True,
        "approval_id": approval_id,
        "approval_message_id": delivery.get("approval_message_id"),
        "delivery_status": delivery.get("delivery_status", "failed"),
    }
    if delivery.get("delivery_id"):
        result["delivery_id"] = delivery["delivery_id"]
    if delivery.get("message"):
        result["message"] = delivery["message"]
    return result


async def recover_pending_approval_cards(
    timeout_seconds: int = 60,
    *,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """补发旧的 pending approval 卡片。"""
    deadline = time.time() - max(0, timeout_seconds)
    approvals = await list_pending_without_message_before(deadline)
    effective_config = config if config is not None else await _load_config()
    results: list[Dict[str, Any]] = []
    for approval in approvals:
        approval_id = str(approval.get("approval_id") or "").strip()
        if not approval_id:
            continue
        result = await publish_or_queue_approval_card(approval_id, config=effective_config)
        results.append(result)
    return {
        "ok": True,
        "scanned": len(approvals),
        "sent": sum(1 for item in results if item.get("delivery_status") == "sent"),
        "pending_retry": sum(1 for item in results if item.get("delivery_status") == "pending_retry"),
        "failed": sum(1 for item in results if item.get("delivery_status") == "failed"),
        "approvals": approvals,
        "results": results,
    }


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
    config = kwargs.get("config") if isinstance(kwargs, dict) else None
    if config is None:
        config = await _load_config()

    result = await request_approval_with_card(
        operation_type=args.get("operation_type", ""),
        command=args.get("command", ""),
        context=args.get("context") or {},
        namespace=args.get("namespace"),
        requester=args.get("requester"),
        risk_level=args.get("risk_level", "standard"),
        incident_id=args.get("incident_id"),
        approval_message_id=args.get("approval_message_id"),
        config=config,
    )
    return json.dumps(result, ensure_ascii=False)


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
