"""Gateway-owned Console Next conversations and agent-run timeline store."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TypeVar

from .evidence_service import SECRET_KEY_RE, redact


JSON = dict[str, Any]
T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50
ACTIVE_CONVERSATION = "active"
ARCHIVED_CONVERSATION = "archived"
DELETED_CONVERSATION = "deleted"
_METRIC_KEYS = {"input_tokens", "output_tokens", "total_tokens"}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL,
    creator TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    incident_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    deleted_at REAL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    runbook_skeleton TEXT NOT NULL DEFAULT 'service_health',
    status TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    incident_id TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    stuck_reason TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (run_id, step_id),
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    thread_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL,
    promoted_from_event_id INTEGER,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id, id);
"""

RUNBOOK_SKELETONS = {"service_health", "k8s_workload", "dependency"}
_TOKEN_PRICE_USD = 0.000002


class AgentRunServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "agent_runs.db"
    return _project_root() / "data" / "agent_runs.db"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _redact_run_value(value: Any) -> Any:
    if isinstance(value, dict):
        if str(value.get("kind") or "").lower() == "secret":
            return {
                key: ("[redacted]" if key in {"data", "stringData"} else _redact_run_value(item))
                for key, item in value.items()
            }
        return {
            key: (_redact_run_value(item) if key in _METRIC_KEYS or not SECRET_KEY_RE.search(str(key)) else "[redacted]")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_run_value(item) for item in value]
    return redact(value)


class AgentRunDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        if self._conn is None:
            return
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
        if "runbook_skeleton" not in columns:
            self._conn.execute("ALTER TABLE agent_runs ADD COLUMN runbook_skeleton TEXT NOT NULL DEFAULT 'service_health'")
        if "metadata_json" not in columns:
            self._conn.execute("ALTER TABLE agent_runs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

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
                        raise sqlite3.ProgrammingError("database connection is closed")
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

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def create_run(self, payload: JSON, *, actor_id: str) -> JSON:
        now = time.time()
        scope = _scope(payload)
        skeleton = _runbook_skeleton(payload)
        conversation_id = f"conv-{uuid.uuid4().hex}"
        run_id = f"run-{uuid.uuid4().hex}"
        title = str(payload.get("title") or payload.get("message") or "Agent Run").strip()[:160] or "Agent Run"
        tags = _tags(payload)
        incident_id = _optional_str(payload.get("incident_id"))
        steps = _runbook_steps(skeleton, scope, payload)
        run_metadata = _run_metadata(steps)
        run_stuck_reason = _run_stuck_reason(run_metadata, payload)
        if run_stuck_reason:
            run_metadata["status"] = "stuck"
            run_metadata["stuck_reason"] = run_stuck_reason
        final_status = str(run_metadata["status"])

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, title, tags_json, status, creator, scope_json,
                    incident_id, created_at, updated_at, archived_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (conversation_id, title, _json_dumps(tags), ACTIVE_CONVERSATION, actor_id, _json_dumps(scope), incident_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, conversation_id, runbook_skeleton, status, scope_json, metadata_json,
                    incident_id, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, conversation_id, skeleton, final_status, _json_dumps(scope), _json_dumps(run_metadata), incident_id, actor_id, now, now),
            )
            _insert_event(conn, run_id, "run_created", "mainline", "Run created", {"scope": scope, "tags": tags, "runbook_skeleton": skeleton}, now, actor_id, None)
            _insert_event(
                conn,
                run_id,
                "run_started",
                "mainline",
                str(payload.get("message") or "Investigation started"),
                {"incident_id": incident_id, "runbook_skeleton": skeleton, "metadata": _zero_metadata("running")},
                now,
                actor_id,
                None,
            )
            for step in steps:
                _insert_step(conn, run_id, step)
                _insert_event(conn, run_id, "agent_phase_changed", "mainline", step["name"], {"step": _event_step(step)}, step["started_at"], "gateway", None)
                for tool_call in step["tool_calls"]:
                    _insert_event(
                        conn,
                        run_id,
                        "tool_call_started",
                        "mainline",
                        str(tool_call["summary"]),
                        {"step_id": step["step_id"], "tool_call": {**tool_call, "status": "running"}},
                        step["started_at"],
                        "gateway",
                        None,
                    )
                    _insert_event(
                        conn,
                        run_id,
                        "tool_call_finished",
                        "mainline",
                        str(tool_call["summary"]),
                        {"step_id": step["step_id"], "tool_call": tool_call, "metadata": step["metadata"]},
                        step["finished_at"],
                        "gateway",
                        None,
                    )
                for evidence_ref in step["evidence_refs"]:
                    _insert_event(
                        conn,
                        run_id,
                        "evidence_added",
                        "mainline",
                        str(evidence_ref["summary"]),
                        {"step_id": step["step_id"], "evidence_ref": evidence_ref, **scope},
                        step["finished_at"],
                        "gateway",
                        None,
                    )
                if step["status"] == "stuck":
                    _insert_event(
                        conn,
                        run_id,
                        "step_stuck",
                        "mainline",
                        step["stuck_reason"] or "Step budget exceeded",
                        {"step": _event_step(step)},
                        step["finished_at"],
                        "gateway",
                        None,
                    )
            if final_status == "stuck":
                _insert_event(conn, run_id, "run_stuck", "mainline", run_stuck_reason or "Run marked stuck", {"metadata": run_metadata}, now + run_metadata["duration_ms"] / 1000, "gateway", None)
            else:
                _insert_event(conn, run_id, "run_finished", "mainline", "Run finished", {"metadata": run_metadata}, now + run_metadata["duration_ms"] / 1000, "gateway", None)

        await asyncio.to_thread(self._execute_write, _write)
        return await self.snapshot(run_id)

    async def list_runs(self) -> list[JSON]:
        def _read() -> list[JSON]:
            rows = self._fetchall(
                """
                SELECT r.run_id, r.conversation_id, r.runbook_skeleton, r.status,
                       r.scope_json, r.metadata_json, r.incident_id,
                       r.created_by, r.created_at, r.updated_at, c.title, c.tags_json,
                       c.status AS conversation_status
                FROM agent_runs r
                JOIN conversations c ON c.conversation_id = r.conversation_id
                WHERE c.status != ?
                ORDER BY r.created_at DESC
                """,
                (DELETED_CONVERSATION,),
            )
            return [_decode_run(row) for row in rows]

        return await asyncio.to_thread(_read)

    async def list_incident_runs(self, incident_id: str) -> list[JSON]:
        runs = await self.list_runs()
        return [run for run in runs if run.get("incident_id") == incident_id]

    async def snapshot(self, run_id: str) -> JSON:
        def _read() -> JSON:
            row = self._fetchone(
                """
                SELECT r.run_id, r.conversation_id, r.runbook_skeleton, r.status,
                       r.scope_json, r.metadata_json, r.incident_id,
                       r.created_by, r.created_at, r.updated_at, c.title, c.tags_json,
                       c.status AS conversation_status, c.creator, c.archived_at, c.deleted_at
                FROM agent_runs r
                JOIN conversations c ON c.conversation_id = r.conversation_id
                WHERE r.run_id = ?
                """,
                (run_id,),
            )
            if row is None:
                raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)
            run = _decode_run(row)
            steps = self._steps_sync(run_id)
            return {
                "conversation": {
                    "conversation_id": run["conversation_id"],
                    "title": run["title"],
                    "tags": run["tags"],
                    "status": run["conversation_status"],
                    "creator": row["creator"],
                    "archived_at": row["archived_at"],
                    "deleted_at": row["deleted_at"],
                },
                "run": run,
                "steps": steps,
                "timeline": self._events_sync(run_id, 0),
                "evidence_refs": _run_evidence_refs(steps),
                "action_refs": [],
                "approval_refs": [],
                "execution_refs": [],
                "permissions": {"can_message": row["deleted_at"] is None, "can_promote": row["deleted_at"] is None},
            }

        return await asyncio.to_thread(_read)

    async def events(self, run_id: str, *, after_id: int = 0) -> list[JSON]:
        def _read() -> list[JSON]:
            self._require_run_sync(run_id)
            return self._events_sync(run_id, after_id)

        return await asyncio.to_thread(_read)

    async def append_message(self, run_id: str, payload: JSON, *, actor_id: str) -> JSON:
        text = str(payload.get("message") or "").strip()
        if not text:
            raise AgentRunServiceError("invalid_request", "message is required", status=HTTPStatus.BAD_REQUEST)
        thread_type = "side" if text.startswith("/btw") else "mainline"
        event_type = "btw_message" if thread_type == "side" else "user_message"
        message = text[4:].strip() if thread_type == "side" else text
        if not message:
            raise AgentRunServiceError("invalid_request", "message is required", status=HTTPStatus.BAD_REQUEST)
        return await self._append_event(run_id, event_type, thread_type, message, {"source": "console"}, actor_id)

    async def promote(self, run_id: str, payload: JSON, *, actor_id: str) -> JSON:
        try:
            source_id = int(payload.get("event_id"))
        except (TypeError, ValueError):
            raise AgentRunServiceError("invalid_request", "event_id is required", status=HTTPStatus.BAD_REQUEST)
        source = await self._event(run_id, source_id)
        if source["thread_type"] != "side":
            raise AgentRunServiceError("invalid_request", "only side-thread events can be promoted", status=HTTPStatus.BAD_REQUEST)
        return await self._append_event(
            run_id,
            "btw_promoted",
            "mainline",
            str(payload.get("message") or source["message"]),
            {"promoted_from_event_id": source_id},
            actor_id,
            promoted_from_event_id=source_id,
        )

    async def update_conversation(self, run_id: str, payload: JSON) -> JSON:
        title = _optional_str(payload.get("title"))
        tags = _tags(payload) if "tags" in payload else None
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            run = _run_row(conn, run_id)
            if run is None:
                raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)
            if title is not None:
                conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?", (title, now, run["conversation_id"]))
            if tags is not None:
                conn.execute("UPDATE conversations SET tags_json = ?, updated_at = ? WHERE conversation_id = ?", (_json_dumps(tags), now, run["conversation_id"]))

        await asyncio.to_thread(self._execute_write, _write)
        return await self.snapshot(run_id)

    async def set_conversation_status(
        self,
        run_id: str,
        status: str,
        *,
        actor_id: str = "gateway",
        reason: str | None = None,
    ) -> JSON:
        now = time.time()
        if status not in {ARCHIVED_CONVERSATION, DELETED_CONVERSATION}:
            raise AgentRunServiceError("invalid_request", "invalid conversation status", status=HTTPStatus.BAD_REQUEST)

        def _write(conn: sqlite3.Connection) -> None:
            run = _run_row(conn, run_id)
            if run is None:
                raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)
            column = "archived_at" if status == ARCHIVED_CONVERSATION else "deleted_at"
            conn.execute(
                f"UPDATE conversations SET status = ?, {column} = ?, updated_at = ? WHERE conversation_id = ?",
                (status, now, now, run["conversation_id"]),
            )
            if status == DELETED_CONVERSATION:
                _insert_event(
                    conn,
                    run_id,
                    "conversation_deleted",
                    "mainline",
                    "Conversation chat content deleted",
                    {
                        "conversation_id": run["conversation_id"],
                        "deleted_by": actor_id,
                        "deleted_at": now,
                        "reason": reason or "deleted from console",
                    },
                    now,
                    actor_id,
                    None,
                )

        await asyncio.to_thread(self._execute_write, _write)
        return await self.snapshot(run_id)

    async def set_run_status(self, run_id: str, status: str, *, actor_id: str, reason: str | None = None) -> JSON:
        now = time.time()
        if status not in {"paused", "terminated", "finished", "stuck"}:
            raise AgentRunServiceError("invalid_request", "invalid run status", status=HTTPStatus.BAD_REQUEST)

        def _write(conn: sqlite3.Connection) -> None:
            if _run_row(conn, run_id) is None:
                raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)
            conn.execute(
                "UPDATE agent_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, now, run_id),
            )
            _insert_event(
                conn,
                run_id,
                f"run_{status}",
                "mainline",
                reason or f"Run {status}",
                {"status": status, "reason": reason},
                now,
                actor_id,
                None,
            )

        await asyncio.to_thread(self._execute_write, _write)
        return await self.snapshot(run_id)

    async def _event(self, run_id: str, event_id: int) -> JSON:
        def _read() -> JSON:
            row = self._fetchone("SELECT * FROM run_events WHERE run_id = ? AND id = ?", (run_id, event_id))
            if row is None:
                raise AgentRunServiceError("not_found", "run event not found", status=HTTPStatus.NOT_FOUND)
            return _decode_event(row)

        return await asyncio.to_thread(_read)

    async def _append_event(
        self,
        run_id: str,
        event_type: str,
        thread_type: str,
        message: str,
        payload: JSON,
        actor_id: str,
        *,
        promoted_from_event_id: int | None = None,
    ) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> int:
            if _run_row(conn, run_id) is None:
                raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)
            return _insert_event(conn, run_id, event_type, thread_type, message, payload, now, actor_id, promoted_from_event_id)

        event_id = await asyncio.to_thread(self._execute_write, _write)
        return await self._event(run_id, event_id)

    def _require_run_sync(self, run_id: str) -> None:
        if self._fetchone("SELECT run_id FROM agent_runs WHERE run_id = ?", (run_id,)) is None:
            raise AgentRunServiceError("not_found", "agent run not found", status=HTTPStatus.NOT_FOUND)

    def _events_sync(self, run_id: str, after_id: int) -> list[JSON]:
        rows = self._fetchall(
            """
            SELECT id, run_id, event_type, thread_type, message, payload_json,
                   created_at, created_by, promoted_from_event_id
            FROM run_events
            WHERE run_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (run_id, max(0, after_id)),
        )
        return [_decode_event(row) for row in rows]

    def _steps_sync(self, run_id: str) -> list[JSON]:
        rows = self._fetchall(
            """
            SELECT run_id, step_id, step_index, name, status, metadata_json,
                   tool_calls_json, evidence_refs_json, stuck_reason, started_at, finished_at
            FROM run_steps
            WHERE run_id = ?
            ORDER BY step_index ASC
            """,
            (run_id,),
        )
        return [_decode_step(row) for row in rows]

    async def deleted_conversation_tombstones(self) -> list[JSON]:
        def _read() -> list[JSON]:
            rows = self._fetchall(
                """
                SELECT e.id, e.run_id, e.created_at, e.created_by, e.payload_json,
                       r.incident_id, r.scope_json, c.conversation_id
                FROM run_events e
                JOIN agent_runs r ON r.run_id = e.run_id
                JOIN conversations c ON c.conversation_id = r.conversation_id
                WHERE e.event_type = 'conversation_deleted'
                ORDER BY e.created_at DESC, e.id DESC
                """
            )
            tombstones: list[JSON] = []
            for row in rows:
                payload = json.loads(row["payload_json"] or "{}")
                scope = json.loads(row["scope_json"] or "{}")
                tombstones.append(
                    {
                        "conversation_id": payload.get("conversation_id") or row["conversation_id"],
                        "run_id": row["run_id"],
                        "incident_id": row["incident_id"],
                        "scope": scope,
                        "deleted_by": payload.get("deleted_by") or row["created_by"],
                        "deleted_at": payload.get("deleted_at") or row["created_at"],
                        "reason": payload.get("reason") or "deleted from console",
                        "event_id": row["id"],
                    }
                )
            return tombstones

        return await asyncio.to_thread(_read)


def _insert_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    thread_type: str,
    message: str,
    payload: JSON,
    created_at: float,
    actor_id: str,
    promoted_from_event_id: int | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO run_events (
            run_id, event_type, thread_type, message, payload_json,
            created_at, created_by, promoted_from_event_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, event_type, thread_type, redact(message), _json_dumps(_redact_run_value(payload)), created_at, actor_id, promoted_from_event_id),
    )
    return int(cursor.lastrowid)


def _insert_step(conn: sqlite3.Connection, run_id: str, step: JSON) -> None:
    conn.execute(
        """
        INSERT INTO run_steps (
            run_id, step_id, step_index, name, status, metadata_json,
            tool_calls_json, evidence_refs_json, stuck_reason, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            step["step_id"],
            step["step_index"],
            step["name"],
            step["status"],
            _json_dumps(_redact_run_value(step["metadata"])),
            _json_dumps(_redact_run_value(step["tool_calls"])),
            _json_dumps(_redact_run_value(step["evidence_refs"])),
            step.get("stuck_reason"),
            step["started_at"],
            step["finished_at"],
        ),
    )


def _run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT run_id, conversation_id FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()


def _scope(payload: JSON) -> JSON:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return {
        "cluster": str(raw.get("cluster") or raw.get("cluster_id") or "").strip(),
        "namespace": str(raw.get("namespace") or "").strip(),
        "service": str(raw.get("service") or raw.get("service_id") or "").strip(),
        "team": str(raw.get("team") or raw.get("team_id") or "").strip(),
        "environment": str(raw.get("environment") or "prod").strip() or "prod",
    }


def _tags(payload: JSON) -> list[str]:
    raw = payload.get("tags") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()][:20]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runbook_skeleton(payload: JSON) -> str:
    skeleton = str(payload.get("runbook_skeleton") or payload.get("skeleton") or "service_health").strip()
    if skeleton not in RUNBOOK_SKELETONS:
        allowed = ", ".join(sorted(RUNBOOK_SKELETONS))
        raise AgentRunServiceError("invalid_runbook_skeleton", f"supported runbook skeletons: {allowed}", status=HTTPStatus.BAD_REQUEST)
    return skeleton


def _runbook_steps(skeleton: str, scope: JSON, payload: JSON) -> list[JSON]:
    names = {
        "service_health": ["service signals", "error budget", "recent changes"],
        "k8s_workload": ["deployment state", "pod health", "k8s events"],
        "dependency": ["dependency graph", "upstream health", "downstream impact"],
    }[skeleton]
    base_tool = {
        "service_health": "openobserve.service_health",
        "k8s_workload": "k8s.workload_read",
        "dependency": "topology.dependency_read",
    }[skeleton]
    step_budget_ms = _payload_int(payload, "step_budget_ms", 1000)
    retry_count = _payload_int(payload, "retry_count", 0)
    max_retries = _payload_int(payload, "max_retries", 3)
    repeated_query = bool(payload.get("repeated_query") or payload.get("repeat_query"))
    started_at = time.time()
    steps: list[JSON] = []
    seen_queries: set[str] = set()
    for index, name in enumerate(names, start=1):
        duration_ms = 120 + index * 35
        input_tokens = 90 + index * 12
        output_tokens = 45 + index * 8
        query_target = f"{scope.get('cluster')}/{scope.get('namespace')}/{scope.get('service')}"
        query_key = f"{base_tool}:{name}:{query_target}"
        if repeated_query and index > 1:
            query_key = next(iter(seen_queries), query_key)
        seen_before = query_key in seen_queries
        seen_queries.add(query_key)
        status = "finished"
        stuck_reason = None
        if duration_ms > step_budget_ms:
            status = "stuck"
            stuck_reason = "step budget exceeded"
        elif retry_count > max_retries:
            status = "stuck"
            stuck_reason = "retry budget exceeded"
        elif seen_before:
            status = "stuck"
            stuck_reason = "repeated query without new evidence"
        metadata = _step_metadata(
            status,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            retry_count=retry_count,
            tool_call_count=1,
        )
        step_id = f"{skeleton}-{index}"
        tool_call = {
            "tool_call_id": f"tool-{step_id}",
            "tool_name": base_tool,
            "target": query_target,
            "summary": f"{name} checked for {scope.get('service') or 'service'}",
            "status": "degraded" if status == "stuck" else "finished",
            "duration_ms": duration_ms,
        }
        evidence_ref = {
            "evidence_id": f"evidence-{step_id}",
            "source": base_tool,
            "summary": f"{name} evidence for {scope.get('service') or 'service'}",
            "scope": scope,
            "step_id": step_id,
        }
        step_started_at = started_at + index / 100
        steps.append(
            {
                "step_id": step_id,
                "step_index": index,
                "name": name,
                "status": status,
                "metadata": metadata,
                "tool_calls": [tool_call],
                "evidence_refs": [] if seen_before else [evidence_ref],
                "stuck_reason": stuck_reason,
                "started_at": step_started_at,
                "finished_at": step_started_at + duration_ms / 1000,
            }
        )
    return steps


def _payload_int(payload: JSON, key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _zero_metadata(status: str) -> JSON:
    return _step_metadata(status, duration_ms=0, input_tokens=0, output_tokens=0, retry_count=0, tool_call_count=0)


def _step_metadata(
    status: str,
    *,
    duration_ms: int,
    input_tokens: int,
    output_tokens: int,
    retry_count: int,
    tool_call_count: int,
) -> JSON:
    total_tokens = input_tokens + output_tokens
    return {
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_tokens * _TOKEN_PRICE_USD, 6),
        "retry_count": retry_count,
        "tool_call_count": tool_call_count,
        "status": status,
    }


def _run_metadata(steps: list[JSON]) -> JSON:
    metadata = [_step.get("metadata", {}) for _step in steps]
    total_duration = sum(int(item.get("duration_ms") or 0) for item in metadata)
    input_tokens = sum(int(item.get("input_tokens") or 0) for item in metadata)
    output_tokens = sum(int(item.get("output_tokens") or 0) for item in metadata)
    tool_calls = sum(int(item.get("tool_call_count") or 0) for item in metadata)
    retry_count = sum(int(item.get("retry_count") or 0) for item in metadata)
    stuck_steps = [step["step_id"] for step in steps if step.get("status") == "stuck"]
    slowest = max(steps, key=lambda step: int(step.get("metadata", {}).get("duration_ms") or 0), default=None)
    status = "stuck" if stuck_steps else "finished"
    total_tokens = input_tokens + output_tokens
    return {
        "duration_ms": total_duration,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_tokens * _TOKEN_PRICE_USD, 6),
        "retry_count": retry_count,
        "tool_call_count": tool_calls,
        "status": status,
        "slowest_step": slowest.get("step_id") if slowest else None,
        "failed_steps": stuck_steps,
        "suspected_loops": stuck_steps,
    }


def _run_stuck_reason(run_metadata: JSON, payload: JSON) -> str | None:
    run_budget_ms = _payload_int(payload, "run_budget_ms", 10_000)
    if int(run_metadata.get("duration_ms") or 0) > run_budget_ms:
        return "run budget exceeded"
    if payload.get("no_progress"):
        return "no progress while run is marked running"
    if run_metadata.get("suspected_loops"):
        return "step stuck"
    return None


def _event_step(step: JSON) -> JSON:
    return {
        "step_id": step["step_id"],
        "name": step["name"],
        "status": step["status"],
        "metadata": step["metadata"],
        "stuck_reason": step.get("stuck_reason"),
    }


def _run_evidence_refs(steps: list[JSON]) -> list[JSON]:
    refs: list[JSON] = []
    for step in steps:
        refs.extend(step.get("evidence_refs") or [])
    return refs


def _decode_run(row: JSON) -> JSON:
    return {
        "run_id": row["run_id"],
        "conversation_id": row["conversation_id"],
        "runbook_skeleton": row["runbook_skeleton"],
        "status": row["status"],
        "scope": json.loads(row["scope_json"] or "{}"),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "incident_id": row["incident_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "title": row["title"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "conversation_status": row["conversation_status"],
    }


def _decode_step(row: JSON) -> JSON:
    return {
        "run_id": row["run_id"],
        "step_id": row["step_id"],
        "step_index": row["step_index"],
        "name": row["name"],
        "status": row["status"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "tool_calls": json.loads(row["tool_calls_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "stuck_reason": row["stuck_reason"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _decode_event(row: JSON) -> JSON:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "event_type": row["event_type"],
        "thread_type": row["thread_type"],
        "message": row["message"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "promoted_from_event_id": row["promoted_from_event_id"],
    }


_DB = AgentRunDB()


async def create_run(payload: JSON, *, actor_id: str) -> JSON:
    return await _DB.create_run(payload, actor_id=actor_id)


async def list_runs() -> list[JSON]:
    return await _DB.list_runs()


async def list_incident_runs(incident_id: str) -> list[JSON]:
    return await _DB.list_incident_runs(incident_id)


async def snapshot(run_id: str) -> JSON:
    return await _DB.snapshot(run_id)


async def events(run_id: str, *, after_id: int = 0) -> list[JSON]:
    return await _DB.events(run_id, after_id=after_id)


async def append_message(run_id: str, payload: JSON, *, actor_id: str) -> JSON:
    return await _DB.append_message(run_id, payload, actor_id=actor_id)


async def promote(run_id: str, payload: JSON, *, actor_id: str) -> JSON:
    return await _DB.promote(run_id, payload, actor_id=actor_id)


async def update_conversation(run_id: str, payload: JSON) -> JSON:
    return await _DB.update_conversation(run_id, payload)


async def archive_conversation(run_id: str) -> JSON:
    return await _DB.set_conversation_status(run_id, ARCHIVED_CONVERSATION)


async def delete_conversation(
    run_id: str,
    payload: JSON | None = None,
    *,
    actor_id: str = "gateway",
) -> JSON:
    payload = payload or {}
    return await _DB.set_conversation_status(
        run_id,
        DELETED_CONVERSATION,
        actor_id=actor_id,
        reason=_optional_str(payload.get("reason")),
    )


async def set_run_status(run_id: str, status: str, *, actor_id: str, reason: str | None = None) -> JSON:
    return await _DB.set_run_status(run_id, status, actor_id=actor_id, reason=reason)


async def append_event(run_id: str, event_type: str, message: str, payload: JSON, *, actor_id: str = "gateway") -> JSON:
    return await _DB._append_event(run_id, event_type, "mainline", message, payload, actor_id)


async def deleted_conversation_tombstones() -> list[JSON]:
    return await _DB.deleted_conversation_tombstones()
