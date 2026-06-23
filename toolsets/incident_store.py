"""事件时间线持久化工具。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - Gateway split image may not ship Hermes
    try:
        from hermes_agent.tools.registry import registry  # type: ignore
    except ImportError:
        class _NoopRegistry:
            def register(self, **_: Any) -> None:
                return None

        registry = _NoopRegistry()


T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50
_INCIDENT_EXTRA_COLUMNS = {
    "service": "TEXT",
    "team": "TEXT",
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
    "diagnosis_summary": "TEXT",
    "diagnosis_confidence": "REAL",
    "diagnosis_level": "TEXT",
    "diagnosis_json": "TEXT",
    "diagnosis_markdown": "TEXT",
    "diagnosed_at": "REAL",
    "service_id": "TEXT",
    "owner_team": "TEXT",
    "ownership_source": "TEXT",
    "ownership_status": "TEXT",
    "ownership_confidence": "REAL",
    "notification_channel": "TEXT",
    "rbac_scope": "TEXT",
    "approval_scope": "TEXT",
}
_CASE_PROFILE_EXTRA_COLUMNS = {
    "root_cause_category": "TEXT",
    "key_evidence_refs_json": "TEXT NOT NULL DEFAULT '[]'",
}
TERMINAL_STATUSES = {"resolved", "closed"}
ACTIVE_STATUSES = {
    "new",
    "triaging",
    "investigating",
    "pending_approval",
    "executing",
    "verifying",
    "rollback_required",
    "abnormal",
}
_ALLOWED_TRANSITIONS = {
    "new": {"triaging", "resolved", "abnormal"},
    "triaging": {"investigating", "resolved", "abnormal"},
    "investigating": {"pending_approval", "executing", "resolved", "abnormal"},
    "pending_approval": {"investigating", "executing", "rollback_required", "abnormal"},
    "executing": {"verifying", "rollback_required", "abnormal"},
    "verifying": {"resolved", "investigating", "rollback_required", "abnormal"},
    "rollback_required": {"investigating", "executing", "abnormal", "closed"},
    "resolved": {"triaging", "closed"},
    "closed": set(),
    "abnormal": {"triaging", "investigating", "closed"},
}
_VALID_EVENT_TYPES = {
    "alert_fired",
    "reopened",
    "triage_start",
    "triage_progress",
    "triage_end",
    "investigate_start",
    "investigate_progress",
    "investigate_end",
    "remediate_proposed",
    "remediate_progress",
    "approval_sent",
    "approval_received",
    "approval_requested",
    "approval_approved",
    "approval_denied",
    "approval_expired",
    "approval_create_failed",
    "approval_skipped",
    "approval_unauthorized",
    "hermes_handoff_requested",
    "hermes_handoff_failed",
    "hermes_handoff_skipped",
    "remediate_executed",
    "remediate_verified",
    "rollback_required",
    "rollback_started",
    "rollback_executed",
    "rollback_failed",
    "verify_progress",
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
    service TEXT,
    team TEXT,
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

CREATE TABLE IF NOT EXISTS incident_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    window_start_ts REAL,
    window_end_ts REAL,
    collected_at REAL NOT NULL,
    collector_version TEXT,
    confidence REAL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_analysis (
    incident_id TEXT PRIMARY KEY,
    symptoms_json TEXT NOT NULL,
    likely_scope TEXT,
    suspected_root_causes_json TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    missing_evidence_json TEXT NOT NULL,
    next_best_actions_json TEXT NOT NULL,
    confidence REAL,
    last_analyzed_at REAL NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_case_profiles (
    incident_id TEXT PRIMARY KEY,
    incident_signature TEXT NOT NULL,
    symptom_fingerprint TEXT,
    final_scope TEXT,
    final_root_cause TEXT,
    root_cause_category TEXT,
    key_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    effective_actions_json TEXT NOT NULL,
    invalid_actions_json TEXT NOT NULL,
    metric_delta_summary_json TEXT NOT NULL,
    change_clue_summary TEXT,
    resolution_seconds REAL,
    similar_incident_ids_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    lesson_key TEXT NOT NULL,
    lesson_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE,
    UNIQUE (incident_id, lesson_key)
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incident_events_incident_id ON incident_events(incident_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_incident_evidence_incident_id ON incident_evidence(incident_id, collected_at, id);
CREATE INDEX IF NOT EXISTS idx_incident_case_profiles_signature ON incident_case_profiles(incident_signature, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_lessons_key ON incident_lessons(lesson_key, updated_at DESC);
"""


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    """返回默认数据库路径。"""
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "incidents.db"
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
        self._ensure_case_profile_columns()

    def _ensure_incident_columns(self) -> None:
        """兼容已存在数据库，补齐 incident 扩展列。"""
        for column, definition in _INCIDENT_EXTRA_COLUMNS.items():
            try:
                self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

    def _ensure_case_profile_columns(self) -> None:
        """兼容已存在数据库，补齐 case_profile 评测集标签列。"""
        for column, definition in _CASE_PROFILE_EXTRA_COLUMNS.items():
            try:
                self._conn.execute(
                    f"ALTER TABLE incident_case_profiles ADD COLUMN {column} {definition}"
                )
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

    def _json_dumps(self, value: Any) -> str:
        """稳定序列化 JSON 字段。"""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    async def create_incident(
        self,
        alert_name: str,
        namespace: str,
        cluster: str,
        summary: str,
        *,
        service: str | None = None,
        team: str | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
        root_message_id: str | None = None,
        thread_id: str | None = None,
        status_card_message_id: str | None = None,
        dedup_key: str | None = None,
        dedup_key_version: str | None = None,
        service_id: str | None = None,
        owner_team: str | None = None,
        ownership_source: str | None = None,
        ownership_status: str | None = None,
        ownership_confidence: float | None = None,
        notification_channel: str | None = None,
        rbac_scope: str | None = None,
        approval_scope: str | None = None,
    ) -> str:
        """创建事件并返回事件 ID。"""
        incident_id = str(uuid.uuid4())
        created_at = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO incidents (
                    id, alert_name, namespace, cluster, service, team, status, created_at, resolved_at, closed_at,
                    summary, platform, chat_id, root_message_id, thread_id, status_card_message_id,
                    dedup_key, dedup_key_version, reopen_count, service_id, owner_team, ownership_source,
                    ownership_status, ownership_confidence, notification_channel, rbac_scope, approval_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    alert_name,
                    namespace,
                    cluster,
                    service,
                    team,
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
                    service_id,
                    owner_team,
                    ownership_source,
                    ownership_status,
                    ownership_confidence,
                    notification_channel,
                    rbac_scope,
                    approval_scope,
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

    async def add_evidence(
        self,
        incident_id: str,
        source_type: str,
        source_ref: str | None,
        summary: str,
        *,
        payload: dict[str, Any] | None = None,
        window_start_ts: float | None = None,
        window_end_ts: float | None = None,
        collected_at: float | None = None,
        collector_version: str | None = None,
        confidence: float | None = None,
    ) -> int:
        """为 incident 追加结构化 evidence。"""
        payload_json = self._json_dumps(payload or {})
        collected = collected_at if collected_at is not None else time.time()

        def _write(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            cursor = conn.execute(
                """
                INSERT INTO incident_evidence (
                    incident_id, source_type, source_ref, summary, payload_json,
                    window_start_ts, window_end_ts, collected_at, collector_version, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    source_type,
                    source_ref,
                    summary,
                    payload_json,
                    window_start_ts,
                    window_end_ts,
                    collected,
                    collector_version,
                    confidence,
                ),
            )
            return int(cursor.lastrowid)

        return await asyncio.to_thread(self._execute_write, _write)

    async def list_evidence(self, incident_id: str) -> list[dict[str, Any]]:
        """列出 incident 的结构化 evidence。"""

        def _read() -> list[dict[str, Any]]:
            rows = self._fetchall(
                """
                SELECT id, incident_id, source_type, source_ref, summary, payload_json,
                       window_start_ts, window_end_ts, collected_at, collector_version, confidence
                FROM incident_evidence
                WHERE incident_id = ?
                ORDER BY collected_at ASC, id ASC
                """,
                (incident_id,),
            )
            for row in rows:
                row["payload"] = json.loads(row.pop("payload_json") or "{}")
            return rows

        return await asyncio.to_thread(_read)

    async def upsert_analysis(
        self,
        incident_id: str,
        *,
        symptoms: list[str],
        likely_scope: str | None = None,
        suspected_root_causes: list[dict[str, Any]] | None = None,
        supporting_evidence: list[dict[str, Any]] | None = None,
        missing_evidence: list[str] | None = None,
        next_best_actions: list[str] | None = None,
        confidence: float | None = None,
        last_analyzed_at: float | None = None,
    ) -> None:
        """写入或更新 incident 的结构化分析。"""
        analyzed_at = last_analyzed_at if last_analyzed_at is not None else time.time()

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            conn.execute(
                """
                INSERT INTO incident_analysis (
                    incident_id, symptoms_json, likely_scope, suspected_root_causes_json,
                    supporting_evidence_json, missing_evidence_json, next_best_actions_json,
                    confidence, last_analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    symptoms_json = excluded.symptoms_json,
                    likely_scope = excluded.likely_scope,
                    suspected_root_causes_json = excluded.suspected_root_causes_json,
                    supporting_evidence_json = excluded.supporting_evidence_json,
                    missing_evidence_json = excluded.missing_evidence_json,
                    next_best_actions_json = excluded.next_best_actions_json,
                    confidence = excluded.confidence,
                    last_analyzed_at = excluded.last_analyzed_at
                """,
                (
                    incident_id,
                    self._json_dumps(symptoms),
                    likely_scope,
                    self._json_dumps(suspected_root_causes or []),
                    self._json_dumps(supporting_evidence or []),
                    self._json_dumps(missing_evidence or []),
                    self._json_dumps(next_best_actions or []),
                    confidence,
                    analyzed_at,
                ),
            )

        await asyncio.to_thread(self._execute_write, _write)

    async def get_analysis(self, incident_id: str) -> dict[str, Any] | None:
        """读取 incident 的结构化分析。"""

        def _read() -> dict[str, Any] | None:
            row = self._fetchone(
                """
                SELECT incident_id, symptoms_json, likely_scope, suspected_root_causes_json,
                       supporting_evidence_json, missing_evidence_json, next_best_actions_json,
                       confidence, last_analyzed_at
                FROM incident_analysis
                WHERE incident_id = ?
                """,
                (incident_id,),
            )
            if row is None:
                return None
            row["symptoms"] = json.loads(row.pop("symptoms_json") or "[]")
            row["suspected_root_causes"] = json.loads(row.pop("suspected_root_causes_json") or "[]")
            row["supporting_evidence"] = json.loads(row.pop("supporting_evidence_json") or "[]")
            row["missing_evidence"] = json.loads(row.pop("missing_evidence_json") or "[]")
            row["next_best_actions"] = json.loads(row.pop("next_best_actions_json") or "[]")
            return row

        return await asyncio.to_thread(_read)

    async def record_incident_diagnosis(
        self,
        incident_id: str,
        diagnosis: dict[str, Any],
        *,
        diagnosed_at: float | None = None,
    ) -> None:
        """Persist AIO-51 diagnosis summary fields on the incident row."""
        recorded_at = diagnosed_at if diagnosed_at is not None else time.time()
        confidence = diagnosis.get("confidence") or {}

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE incidents
                SET diagnosis_summary = ?,
                    diagnosis_confidence = ?,
                    diagnosis_level = ?,
                    diagnosis_json = ?,
                    diagnosis_markdown = ?,
                    diagnosed_at = ?
                WHERE id = ?
                """,
                (
                    diagnosis.get("summary"),
                    confidence.get("score"),
                    confidence.get("level"),
                    self._json_dumps(diagnosis),
                    diagnosis.get("markdown"),
                    recorded_at,
                    incident_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"事件不存在: {incident_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def upsert_incident_lesson(
        self,
        incident_id: str,
        lesson_key: str,
        lesson: dict[str, Any],
        *,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> None:
        """Insert or update a reusable incident lesson."""
        created = created_at if created_at is not None else time.time()
        updated = updated_at if updated_at is not None else time.time()

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            existing = conn.execute(
                "SELECT created_at FROM incident_lessons WHERE incident_id = ? AND lesson_key = ?",
                (incident_id, lesson_key),
            ).fetchone()
            effective_created = float(existing["created_at"]) if existing is not None else created
            conn.execute(
                """
                INSERT INTO incident_lessons (
                    incident_id, lesson_key, lesson_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(incident_id, lesson_key) DO UPDATE SET
                    lesson_json = excluded.lesson_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (incident_id, lesson_key, self._json_dumps(lesson), effective_created, updated),
            )

        await asyncio.to_thread(self._execute_write, _write)

    async def list_incident_lessons(
        self,
        *,
        incident_id: str | None = None,
        lesson_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List stored incident lessons."""

        def _read() -> list[dict[str, Any]]:
            params: list[Any] = []
            sql = """
                SELECT id, incident_id, lesson_key, lesson_json, created_at, updated_at
                FROM incident_lessons
                WHERE 1 = 1
            """
            if incident_id is not None:
                sql += " AND incident_id = ?"
                params.append(incident_id)
            if lesson_key is not None:
                sql += " AND lesson_key = ?"
                params.append(lesson_key)
            sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
            params.append(limit)
            rows = self._fetchall(sql, tuple(params))
            for row in rows:
                row["lesson"] = json.loads(row.pop("lesson_json") or "{}")
            return rows

        return await asyncio.to_thread(_read)

    async def upsert_case_profile(
        self,
        incident_id: str,
        *,
        incident_signature: str,
        symptom_fingerprint: str | None = None,
        final_scope: str | None = None,
        final_root_cause: str | None = None,
        root_cause_category: str | None = None,
        key_evidence_refs: list[str] | None = None,
        effective_actions: list[str] | None = None,
        invalid_actions: list[str] | None = None,
        metric_delta_summary: dict[str, Any] | None = None,
        change_clue_summary: str | None = None,
        resolution_seconds: float | None = None,
        similar_incident_ids: list[str] | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> None:
        """写入或更新 resolved incident 的 case profile。"""
        created = created_at if created_at is not None else time.time()
        updated = updated_at if updated_at is not None else time.time()

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                raise ValueError(f"事件不存在: {incident_id}")
            existing = conn.execute(
                "SELECT created_at FROM incident_case_profiles WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            effective_created = float(existing["created_at"]) if existing is not None else created
            conn.execute(
                """
                INSERT INTO incident_case_profiles (
                    incident_id, incident_signature, symptom_fingerprint, final_scope,
                    final_root_cause, root_cause_category, key_evidence_refs_json,
                    effective_actions_json, invalid_actions_json,
                    metric_delta_summary_json, change_clue_summary, resolution_seconds,
                    similar_incident_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    incident_signature = excluded.incident_signature,
                    symptom_fingerprint = excluded.symptom_fingerprint,
                    final_scope = excluded.final_scope,
                    final_root_cause = excluded.final_root_cause,
                    root_cause_category = excluded.root_cause_category,
                    key_evidence_refs_json = excluded.key_evidence_refs_json,
                    effective_actions_json = excluded.effective_actions_json,
                    invalid_actions_json = excluded.invalid_actions_json,
                    metric_delta_summary_json = excluded.metric_delta_summary_json,
                    change_clue_summary = excluded.change_clue_summary,
                    resolution_seconds = excluded.resolution_seconds,
                    similar_incident_ids_json = excluded.similar_incident_ids_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    incident_id,
                    incident_signature,
                    symptom_fingerprint,
                    final_scope,
                    final_root_cause,
                    root_cause_category,
                    self._json_dumps(key_evidence_refs or []),
                    self._json_dumps(effective_actions or []),
                    self._json_dumps(invalid_actions or []),
                    self._json_dumps(metric_delta_summary or {}),
                    change_clue_summary,
                    resolution_seconds,
                    self._json_dumps(similar_incident_ids or []),
                    effective_created,
                    updated,
                ),
            )

        await asyncio.to_thread(self._execute_write, _write)

    async def get_case_profile(self, incident_id: str) -> dict[str, Any] | None:
        """读取指定 incident 的 case profile。"""

        def _read() -> dict[str, Any] | None:
            row = self._fetchone(
                """
                SELECT incident_id, incident_signature, symptom_fingerprint, final_scope,
                       final_root_cause, root_cause_category, key_evidence_refs_json,
                       effective_actions_json, invalid_actions_json,
                       metric_delta_summary_json, change_clue_summary, resolution_seconds,
                       similar_incident_ids_json, created_at, updated_at
                FROM incident_case_profiles
                WHERE incident_id = ?
                """,
                (incident_id,),
            )
            if row is None:
                return None
            row["effective_actions"] = json.loads(row.pop("effective_actions_json") or "[]")
            row["invalid_actions"] = json.loads(row.pop("invalid_actions_json") or "[]")
            row["metric_delta_summary"] = json.loads(row.pop("metric_delta_summary_json") or "{}")
            row["similar_incident_ids"] = json.loads(row.pop("similar_incident_ids_json") or "[]")
            row["key_evidence_refs"] = json.loads(row.pop("key_evidence_refs_json") or "[]")
            return row

        return await asyncio.to_thread(_read)

    async def list_recent_case_profiles(
        self,
        *,
        namespace: str,
        final_scope: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按 namespace 列出最近 resolved case profile。"""

        def _read() -> list[dict[str, Any]]:
            params: list[Any] = [namespace]
            sql = (
                """
                SELECT p.incident_id, p.incident_signature, p.symptom_fingerprint, p.final_scope,
                       p.final_root_cause, p.root_cause_category, p.key_evidence_refs_json,
                       p.effective_actions_json, p.invalid_actions_json,
                       p.metric_delta_summary_json, p.change_clue_summary, p.resolution_seconds,
                       p.similar_incident_ids_json, p.created_at, p.updated_at
                FROM incident_case_profiles AS p
                JOIN incidents AS i ON i.id = p.incident_id
                WHERE i.namespace = ?
                """
            )
            if final_scope is not None:
                sql += " AND p.final_scope = ?"
                params.append(final_scope)
            sql += " ORDER BY p.updated_at DESC LIMIT ?"
            params.append(limit)
            rows = self._fetchall(sql, tuple(params))
            return [self._decode_case_profile_row(row) for row in rows]

        return await asyncio.to_thread(_read)

    async def find_similar_case_profiles(
        self,
        incident_signature: str,
        *,
        exclude_incident_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按 incident signature 查找相似 case。"""

        def _read() -> list[dict[str, Any]]:
            params: list[Any] = [incident_signature]
            sql = (
                """
                SELECT incident_id, incident_signature, symptom_fingerprint, final_scope,
                       final_root_cause, root_cause_category, key_evidence_refs_json,
                       effective_actions_json, invalid_actions_json,
                       metric_delta_summary_json, change_clue_summary, resolution_seconds,
                       similar_incident_ids_json, created_at, updated_at
                FROM incident_case_profiles
                WHERE incident_signature = ?
                """
            )
            if exclude_incident_id is not None:
                sql += " AND incident_id != ?"
                params.append(exclude_incident_id)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = self._fetchall(sql, tuple(params))
            return [self._decode_case_profile_row(row) for row in rows]

        return await asyncio.to_thread(_read)

    def _decode_case_profile_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """反序列化 case profile 行。"""
        decoded = dict(row)
        decoded["effective_actions"] = json.loads(decoded.pop("effective_actions_json") or "[]")
        decoded["invalid_actions"] = json.loads(decoded.pop("invalid_actions_json") or "[]")
        decoded["metric_delta_summary"] = json.loads(decoded.pop("metric_delta_summary_json") or "{}")
        decoded["similar_incident_ids"] = json.loads(decoded.pop("similar_incident_ids_json") or "[]")
        decoded["key_evidence_refs"] = json.loads(decoded.pop("key_evidence_refs_json") or "[]")
        return decoded

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

    async def mark_rollback_required(
        self,
        incident_id: str,
        *,
        reason_code: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        tool_name: str = "remediation_health",
    ) -> int:
        """标记 incident 需要人工判断 rollback，并写入时间线。"""

        incident = await self.get_incident(incident_id)
        previous_status = incident["status"]
        if incident["status"] != "rollback_required":
            await self.update_status(incident_id, "rollback_required")

        event_metadata = dict(metadata or {})
        event_metadata["reason_code"] = reason_code
        event_metadata["previous_status"] = previous_status
        return await self.add_event(
            incident_id,
            "rollback_required",
            tool_name,
            "post-remediation health check failed",
            summary,
            event_metadata,
        )

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

    async def update_feishu_binding(
        self,
        incident_id: str,
        *,
        chat_id: str,
        root_message_id: str | None = None,
        thread_id: str | None = None,
        status_card_message_id: str | None = None,
    ) -> None:
        """回写 incident 的飞书会话绑定。"""

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE incidents
                SET platform = 'feishu',
                    chat_id = ?,
                    root_message_id = ?,
                    thread_id = ?,
                    status_card_message_id = ?
                WHERE id = ?
                """,
                (chat_id, root_message_id, thread_id, status_card_message_id, incident_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"事件不存在: {incident_id}")

        await asyncio.to_thread(self._execute_write, _write)

    async def find_by_feishu_context(
        self,
        *,
        chat_id: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """按飞书 chat/thread/message 上下文反查 incident。"""

        def _read() -> dict[str, Any] | None:
            if chat_id and thread_id:
                row = self._fetchone(
                    """
                    SELECT *
                    FROM incidents
                    WHERE platform = 'feishu' AND chat_id = ? AND thread_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (chat_id, thread_id),
                )
                if row is not None:
                    return row

            if message_id:
                return self._fetchone(
                    """
                    SELECT *
                    FROM incidents
                    WHERE platform = 'feishu'
                      AND (root_message_id = ? OR status_card_message_id = ? OR thread_id = ?)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (message_id, message_id, message_id),
                )
            return None

        return await asyncio.to_thread(_read)

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
            "service": {"type": "string", "description": "服务名，用于 RBAC scope"},
            "team": {"type": "string", "description": "团队名，用于 RBAC scope"},
            "summary": {"type": "string", "description": "事件摘要"},
            "platform": {"type": "string", "description": "平台名称"},
            "chat_id": {"type": "string", "description": "群聊 ID"},
            "root_message_id": {"type": "string", "description": "根消息 ID"},
            "thread_id": {"type": "string", "description": "Thread ID"},
            "status_card_message_id": {"type": "string", "description": "状态卡片消息 ID"},
            "dedup_key": {"type": "string", "description": "告警去重键"},
            "dedup_key_version": {"type": "string", "description": "去重键版本"},
            "service_id": {"type": "string", "description": "CMDB 服务 ID 或规范化服务键"},
            "owner_team": {"type": "string", "description": "服务所属团队"},
            "ownership_source": {"type": "string", "description": "归属来源，如 bk_cmdb/cache/default_team"},
            "ownership_status": {"type": "string", "description": "归属状态 owned/unowned"},
            "ownership_confidence": {"type": "number", "description": "归属解析置信度"},
            "notification_channel": {"type": "string", "description": "团队通知通道"},
            "rbac_scope": {"type": "string", "description": "权限作用域"},
            "approval_scope": {"type": "string", "description": "审批作用域"},
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
    service: str | None = None,
    team: str | None = None,
    platform: str | None = None,
    chat_id: str | None = None,
    root_message_id: str | None = None,
    thread_id: str | None = None,
    status_card_message_id: str | None = None,
    dedup_key: str | None = None,
    dedup_key_version: str | None = None,
    service_id: str | None = None,
    owner_team: str | None = None,
    ownership_source: str | None = None,
    ownership_status: str | None = None,
    ownership_confidence: float | None = None,
    notification_channel: str | None = None,
    rbac_scope: str | None = None,
    approval_scope: str | None = None,
) -> str:
    """创建事件。"""
    return await _STORE.create_incident(
        alert_name,
        namespace,
        cluster,
        summary,
        service=service,
        team=team,
        platform=platform,
        chat_id=chat_id,
        root_message_id=root_message_id,
        thread_id=thread_id,
        status_card_message_id=status_card_message_id,
        dedup_key=dedup_key,
        dedup_key_version=dedup_key_version,
        service_id=service_id,
        owner_team=owner_team,
        ownership_source=ownership_source,
        ownership_status=ownership_status,
        ownership_confidence=ownership_confidence,
        notification_channel=notification_channel,
        rbac_scope=rbac_scope,
        approval_scope=approval_scope,
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


async def add_evidence(
    incident_id: str,
    source_type: str,
    source_ref: str | None,
    summary: str,
    *,
    payload: dict[str, Any] | None = None,
    window_start_ts: float | None = None,
    window_end_ts: float | None = None,
    collected_at: float | None = None,
    collector_version: str | None = None,
    confidence: float | None = None,
) -> int:
    """追加结构化 evidence。"""
    return await _STORE.add_evidence(
        incident_id,
        source_type,
        source_ref,
        summary,
        payload=payload,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        collected_at=collected_at,
        collector_version=collector_version,
        confidence=confidence,
    )


async def list_evidence(incident_id: str) -> list[dict[str, Any]]:
    """列出结构化 evidence。"""
    return await _STORE.list_evidence(incident_id)


async def upsert_analysis(
    incident_id: str,
    *,
    symptoms: list[str],
    likely_scope: str | None = None,
    suspected_root_causes: list[dict[str, Any]] | None = None,
    supporting_evidence: list[dict[str, Any]] | None = None,
    missing_evidence: list[str] | None = None,
    next_best_actions: list[str] | None = None,
    confidence: float | None = None,
    last_analyzed_at: float | None = None,
) -> None:
    """写入结构化 analysis。"""
    await _STORE.upsert_analysis(
        incident_id,
        symptoms=symptoms,
        likely_scope=likely_scope,
        suspected_root_causes=suspected_root_causes,
        supporting_evidence=supporting_evidence,
        missing_evidence=missing_evidence,
        next_best_actions=next_best_actions,
        confidence=confidence,
        last_analyzed_at=last_analyzed_at,
    )


async def get_analysis(incident_id: str) -> dict[str, Any] | None:
    """读取结构化 analysis。"""
    return await _STORE.get_analysis(incident_id)


async def record_incident_diagnosis(
    incident_id: str,
    diagnosis: dict[str, Any],
    *,
    diagnosed_at: float | None = None,
) -> None:
    """写入 AIO-51 diagnosis 摘要字段。"""
    await _STORE.record_incident_diagnosis(incident_id, diagnosis, diagnosed_at=diagnosed_at)


async def upsert_incident_lesson(
    incident_id: str,
    lesson_key: str,
    lesson: dict[str, Any],
    *,
    created_at: float | None = None,
    updated_at: float | None = None,
) -> None:
    """写入或更新 incident lesson。"""
    await _STORE.upsert_incident_lesson(
        incident_id,
        lesson_key,
        lesson,
        created_at=created_at,
        updated_at=updated_at,
    )


async def list_incident_lessons(
    *,
    incident_id: str | None = None,
    lesson_key: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列出 incident lessons。"""
    return await _STORE.list_incident_lessons(incident_id=incident_id, lesson_key=lesson_key, limit=limit)


async def upsert_case_profile(
    incident_id: str,
    *,
    incident_signature: str,
    symptom_fingerprint: str | None = None,
    final_scope: str | None = None,
    final_root_cause: str | None = None,
    root_cause_category: str | None = None,
    key_evidence_refs: list[str] | None = None,
    effective_actions: list[str] | None = None,
    invalid_actions: list[str] | None = None,
    metric_delta_summary: dict[str, Any] | None = None,
    change_clue_summary: str | None = None,
    resolution_seconds: float | None = None,
    similar_incident_ids: list[str] | None = None,
    created_at: float | None = None,
    updated_at: float | None = None,
) -> None:
    """写入 resolved case profile。"""
    await _STORE.upsert_case_profile(
        incident_id,
        incident_signature=incident_signature,
        symptom_fingerprint=symptom_fingerprint,
        final_scope=final_scope,
        final_root_cause=final_root_cause,
        root_cause_category=root_cause_category,
        key_evidence_refs=key_evidence_refs,
        effective_actions=effective_actions,
        invalid_actions=invalid_actions,
        metric_delta_summary=metric_delta_summary,
        change_clue_summary=change_clue_summary,
        resolution_seconds=resolution_seconds,
        similar_incident_ids=similar_incident_ids,
        created_at=created_at,
        updated_at=updated_at,
    )


async def get_case_profile(incident_id: str) -> dict[str, Any] | None:
    """读取 resolved case profile。"""
    return await _STORE.get_case_profile(incident_id)


async def list_recent_case_profiles(
    *,
    namespace: str,
    final_scope: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """列出最近 resolved case profile。"""
    return await _STORE.list_recent_case_profiles(namespace=namespace, final_scope=final_scope, limit=limit)


async def find_similar_case_profiles(
    incident_signature: str,
    *,
    exclude_incident_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """查找相似 resolved case profile。"""
    return await _STORE.find_similar_case_profiles(
        incident_signature,
        exclude_incident_id=exclude_incident_id,
        limit=limit,
    )


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


async def mark_rollback_required(
    incident_id: str,
    *,
    reason_code: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
    tool_name: str = "remediation_health",
) -> int:
    """标记 incident 需要人工判断 rollback，并写入时间线。"""
    return await _STORE.mark_rollback_required(
        incident_id,
        reason_code=reason_code,
        summary=summary,
        metadata=metadata,
        tool_name=tool_name,
    )


async def update_operator(incident_id: str, operator: str) -> None:
    """更新事件负责人。"""
    await _STORE.update_operator(incident_id, operator)


async def update_feishu_binding(
    incident_id: str,
    *,
    chat_id: str,
    root_message_id: str | None = None,
    thread_id: str | None = None,
    status_card_message_id: str | None = None,
) -> None:
    """回写 incident 的飞书会话绑定。"""
    await _STORE.update_feishu_binding(
        incident_id,
        chat_id=chat_id,
        root_message_id=root_message_id,
        thread_id=thread_id,
        status_card_message_id=status_card_message_id,
    )


async def find_by_feishu_context(
    *,
    chat_id: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any] | None:
    """按飞书会话上下文反查 incident。"""
    return await _STORE.find_by_feishu_context(chat_id=chat_id, thread_id=thread_id, message_id=message_id)


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
        service=args.get("service"),
        team=args.get("team"),
        platform=args.get("platform"),
        chat_id=args.get("chat_id"),
        root_message_id=args.get("root_message_id"),
        thread_id=args.get("thread_id"),
        status_card_message_id=args.get("status_card_message_id"),
        dedup_key=args.get("dedup_key"),
        dedup_key_version=args.get("dedup_key_version"),
        service_id=args.get("service_id"),
        owner_team=args.get("owner_team"),
        ownership_source=args.get("ownership_source"),
        ownership_status=args.get("ownership_status"),
        ownership_confidence=args.get("ownership_confidence"),
        notification_channel=args.get("notification_channel"),
        rbac_scope=args.get("rbac_scope"),
        approval_scope=args.get("approval_scope"),
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
    incident_id = args.get("incident_id", "")
    incident = await get_incident(incident_id)
    timeline = await get_timeline(incident_id)
    return json.dumps(
        {
            "incident": _summarize_incident_for_timeline(incident),
            "readable_summary": _build_timeline_readable_summary(incident, timeline),
            "reply_guidance": "请优先说明这是历史时间线，并结合当前 incident 状态作答。",
            "events": timeline,
        },
        ensure_ascii=False,
    )


def _summarize_incident_for_timeline(incident: dict[str, Any]) -> dict[str, Any]:
    """提取 timeline 回答所需的 incident 主状态。"""
    keys = [
        "id",
        "alert_name",
        "namespace",
        "cluster",
        "service",
        "team",
        "status",
        "platform",
        "chat_id",
        "thread_id",
        "root_message_id",
        "status_card_message_id",
        "summary",
    ]
    return {key: incident.get(key) for key in keys}


def _format_timestamp(timestamp: Any) -> str:
    """格式化 Unix timestamp，保留原值以便排障核对。"""
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return str(timestamp or "unknown")
    return f"{datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')} ({value:.3f})"


def _build_timeline_readable_summary(incident: dict[str, Any], timeline: list[dict[str, Any]]) -> str:
    """构建适合模型直接转述的 timeline 摘要。"""
    lines = [
        f"Incident: {incident.get('id', '')} {incident.get('alert_name', '')}",
        f"当前状态: {incident.get('status', '')}",
    ]
    chat_id = incident.get("chat_id") or "未绑定"
    thread_id = incident.get("thread_id") or "未绑定"
    lines.append(f"飞书会话: chat_id={chat_id}, thread_id={thread_id}")

    if not timeline:
        lines.append("历史记录: 暂无事件。")
        return "\n".join(lines)

    lines.append("历史记录:")
    for index, event in enumerate(timeline, start=1):
        output = event.get("output_summary") or event.get("input_summary") or ""
        lines.append(
            f"{index}. [{_format_timestamp(event.get('timestamp'))}] "
            f"{event.get('event_type', '')} / {event.get('tool_name', '')}: {output}"
        )
    lines.append("提示: 历史记录描述当时发生的排查过程，不等同于当前仍存在的问题。")
    return "\n".join(lines)


async def _tool_incident_list_active(args: dict[str, Any], **_: Any) -> str:
    """工具入口：列出活跃事件。"""
    del args
    return json.dumps(await list_active(), ensure_ascii=False)


registry.register(name="incident_create", toolset="sre", schema=INCIDENT_CREATE_SCHEMA, handler=_tool_incident_create, is_async=True)
registry.register(name="incident_add_event", toolset="sre", schema=INCIDENT_ADD_EVENT_SCHEMA, handler=_tool_incident_add_event, is_async=True)
registry.register(name="incident_timeline", toolset="sre", schema=INCIDENT_TIMELINE_SCHEMA, handler=_tool_incident_timeline, is_async=True)
registry.register(name="incident_list_active", toolset="sre", schema=INCIDENT_LIST_ACTIVE_SCHEMA, handler=_tool_incident_list_active, is_async=True)
