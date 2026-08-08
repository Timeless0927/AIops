"""Gateway-owned Chat Attachment storage, validation and scanning."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import posixpath
import socket
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Callable

import yaml

from .chat_content_safety import contains_sensitive
from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_MESSAGE_ATTACHMENTS = 5
MAX_EXTRACTED_BYTES = 1 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
_SCHEMA_VERSION = 53
_SCHEMA = """
CREATE TABLE chat_attachment_blobs (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    size INTEGER NOT NULL CHECK (size > 0)
);
CREATE TABLE chat_attachments (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    declared_size INTEGER NOT NULL CHECK (declared_size > 0 AND declared_size <= 20971520),
    size INTEGER CHECK (size IS NULL OR (size > 0 AND size <= 20971520)),
    sha256 TEXT REFERENCES chat_attachment_blobs(sha256),
    status TEXT NOT NULL CHECK (status IN ('pending', 'uploading', 'scanning', 'ready', 'rejected', 'failed')),
    rejection_code TEXT,
    extracted_text TEXT,
    reserve_key TEXT NOT NULL,
    reserve_hash TEXT NOT NULL CHECK (length(reserve_hash) = 64),
    message_id TEXT REFERENCES chat_messages(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(owner_id, reserve_key)
);
CREATE INDEX chat_attachments_session ON chat_attachments(owner_id, session_id, created_at, id);
CREATE INDEX chat_attachments_search ON chat_attachments(owner_id, filename);
CREATE TABLE chat_attachment_operations (
    owner_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(owner_id, idempotency_key)
);
CREATE TABLE chat_attachment_gc (
    sha256 TEXT PRIMARY KEY REFERENCES chat_attachment_blobs(sha256) ON DELETE CASCADE,
    queued_at REAL NOT NULL
);
CREATE TRIGGER chat_attachment_gc_delete AFTER DELETE ON chat_attachments
WHEN OLD.sha256 IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO chat_attachment_gc(sha256, queued_at) VALUES (OLD.sha256, strftime('%s', 'now'));
END;
CREATE TRIGGER chat_attachment_gc_replace AFTER UPDATE OF sha256 ON chat_attachments
WHEN OLD.sha256 IS NOT NULL AND OLD.sha256 <> NEW.sha256
BEGIN
    INSERT OR IGNORE INTO chat_attachment_gc(sha256, queued_at) VALUES (OLD.sha256, strftime('%s', 'now'));
END;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))
register_migrations((
    (54, """
        ALTER TABLE chat_attachments ADD COLUMN parse_state TEXT NOT NULL DEFAULT 'pending';
        ALTER TABLE chat_attachments ADD COLUMN extracted_sha256 TEXT;
        ALTER TABLE chat_attachments ADD COLUMN model_use_status TEXT NOT NULL DEFAULT 'not_used';
    """),
))


class ChatAttachmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


Scanner = Callable[[bytes], bool]

_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".png": ("image/png",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".webp": ("image/webp",),
    ".pdf": ("application/pdf",),
    ".txt": ("text/plain",),
    ".log": ("text/plain",),
    ".md": ("text/markdown", "text/plain"),
    ".markdown": ("text/markdown", "text/plain"),
    ".json": ("application/json",),
    ".yaml": ("application/yaml", "text/yaml", "application/x-yaml", "text/x-yaml"),
    ".yml": ("application/yaml", "text/yaml", "application/x-yaml", "text/x-yaml"),
    ".csv": ("text/csv",),
}


class ChatAttachments:
    """Own attachment metadata and bytes; callers provide only scanner I/O seam."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        scanner: Scanner | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        data_dir: Path | str | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory
        root = Path(data_dir) if data_dir is not None else self._database.db_path.parent / "chat-attachments"
        self._root = root.expanduser()
        self._quarantine = self._root / "quarantine"
        self._blobs = self._root / "blobs"
        self._scanner = scanner or _clamav_scan

    def reserve(
        self,
        owner_id: str,
        session_id: str,
        *,
        filename: str,
        content_type: str,
        declared_size: int,
        idempotency_key: str,
    ) -> JSON:
        owner_id = _text(owner_id, "owner_id", 200)
        session_id = _text(session_id, "session_id", 200)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        filename = _filename(filename)
        content_type = _content_type(content_type)
        extension = Path(filename).suffix.lower()
        if extension not in _EXTENSIONS or content_type not in _EXTENSIONS[extension]:
            raise ChatAttachmentError("unsupported_type", "文件类型或 MIME 不受支持")
        lower_filename = filename.lower()
        if any(token in lower_filename for token in ("kubeconfig", "credential", "password", "secret")):
            raise ChatAttachmentError("sensitive_filename", "疑似凭据或证书文件不允许上传")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size <= 0:
            raise ChatAttachmentError("invalid_size", "文件大小无效")
        if declared_size > MAX_FILE_BYTES:
            raise ChatAttachmentError("attachment_too_large", "单个附件不能超过 20MB")
        now = self._clock()
        request_hash = _hash({"filename": filename, "content_type": content_type, "size": declared_size})
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._session(conn, owner_id, session_id)
            replay = conn.execute(
                "SELECT reserve_hash, id FROM chat_attachments WHERE owner_id = ? AND reserve_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["reserve_hash"]) != request_hash:
                    raise ChatAttachmentError("idempotency_conflict", "附件请求与已接受请求冲突")
                return self._view(conn, owner_id, session_id, str(replay["id"]))
            count, total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(declared_size), 0) FROM chat_attachments "
                "WHERE owner_id = ? AND session_id = ? AND message_id IS NULL AND status NOT IN ('rejected', 'failed')",
                (owner_id, session_id),
            ).fetchone()
            if int(count) >= MAX_MESSAGE_ATTACHMENTS:
                raise ChatAttachmentError("attachment_count_limit", "每条消息最多上传 5 个附件")
            if int(total) + declared_size > MAX_MESSAGE_BYTES:
                raise ChatAttachmentError("attachment_total_too_large", "单条消息附件总大小不能超过 50MB")
            attachment_id = self._id_factory()
            conn.execute(
                """INSERT INTO chat_attachments
                   (id, owner_id, session_id, filename, content_type, declared_size, status,
                    reserve_key, reserve_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (attachment_id, owner_id, session_id, filename, content_type, declared_size, idempotency_key, request_hash, now, now),
            )
            _append_event(conn, session_id, "attachment.updated", f"attachment:{attachment_id}:pending", {"attachment_id": attachment_id, "status": "pending"}, now)
            return self._view(conn, owner_id, session_id, attachment_id)

    def upload(
        self,
        owner_id: str,
        session_id: str,
        attachment_id: str,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> JSON:
        owner_id = _text(owner_id, "owner_id", 200)
        session_id = _text(session_id, "session_id", 200)
        attachment_id = _text(attachment_id, "attachment_id", 200)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        if not isinstance(content, bytes):
            raise ChatAttachmentError("invalid_content", "附件内容必须是二进制")
        request_hash = hashlib.sha256(content).hexdigest()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._owned(conn, owner_id, session_id, attachment_id)
            replay = conn.execute(
                "SELECT request_hash, response_json FROM chat_attachment_operations WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatAttachmentError("idempotency_conflict", "附件上传与已接受请求冲突")
                return json.loads(str(replay["response_json"]))
            if row["status"] == "ready" and row["sha256"] == request_hash:
                response = self._view(conn, owner_id, session_id, attachment_id)
                self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, now)
                return response
            if row["status"] == "ready":
                raise ChatAttachmentError("attachment_immutable", "已就绪附件不能替换")
            if row["status"] in {"uploading", "scanning"}:
                raise ChatAttachmentError("attachment_in_progress", "附件正在处理中")
            if len(content) > MAX_FILE_BYTES:
                response = self._set_state(conn, row, "rejected", "attachment_too_large", now)
                self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, now)
                (self._quarantine / attachment_id).unlink(missing_ok=True)
                return response
            conn.execute("UPDATE chat_attachments SET status = 'uploading', rejection_code = NULL, updated_at = ? WHERE id = ?", (now, attachment_id))
            _append_event(conn, session_id, "attachment.updated", f"attachment:{attachment_id}:uploading:{idempotency_key}", {"attachment_id": attachment_id, "status": "uploading"}, now)
        quarantine = self._quarantine / attachment_id
        try:
            self._quarantine.mkdir(parents=True, exist_ok=True)
            quarantine.write_bytes(content)
        except OSError:
            quarantine.unlink(missing_ok=True)
            return self._complete_failure(owner_id, session_id, attachment_id, idempotency_key, request_hash, "failed", "storage_unavailable")
        try:
            with self._database.connect() as conn:
                row = self._owned(conn, owner_id, session_id, attachment_id)
                scan_now = self._clock()
                conn.execute("UPDATE chat_attachments SET status = 'scanning', updated_at = ? WHERE id = ?", (scan_now, attachment_id))
                _append_event(conn, session_id, "attachment.updated", f"attachment:{attachment_id}:scanning:{idempotency_key}", {"attachment_id": attachment_id, "status": "scanning"}, scan_now)
            try:
                clean = bool(self._scanner(content))
            except (OSError, TimeoutError, socket.timeout, ConnectionError):
                return self._complete_failure(owner_id, session_id, attachment_id, idempotency_key, request_hash, "failed", "scanner_unavailable")
            except Exception:
                return self._complete_failure(owner_id, session_id, attachment_id, idempotency_key, request_hash, "failed", "scanner_error")
            if not clean:
                return self._complete_failure(owner_id, session_id, attachment_id, idempotency_key, request_hash, "rejected", "malware_detected", remove=True)
            try:
                with self._database.connect() as conn:
                    conn.execute(
                        "UPDATE chat_attachments SET parse_state = 'parsing', updated_at = ? WHERE id = ?",
                        (self._clock(), attachment_id),
                    )
                extracted = _validate_and_extract(str(row["filename"]), str(row["content_type"]), content)
            except ChatAttachmentError as exc:
                return self._complete_failure(owner_id, session_id, attachment_id, idempotency_key, request_hash, "rejected", exc.code, remove=True)
            digest = request_hash
            with self._database.connect() as conn:
                row = self._owned(conn, owner_id, session_id, attachment_id)
                conn.execute("INSERT OR IGNORE INTO chat_attachment_blobs(sha256, size) VALUES (?, ?)", (digest, len(content)))
                blob = self._blobs / digest
                self._blobs.mkdir(parents=True, exist_ok=True)
                if not blob.exists():
                    try:
                        blob.write_bytes(content)
                    except OSError:
                        conn.execute("DELETE FROM chat_attachment_blobs WHERE sha256 = ? AND NOT EXISTS (SELECT 1 FROM chat_attachments WHERE sha256 = ?)", (digest, digest))
                        response = self._set_state(conn, row, "failed", "storage_unavailable", self._clock())
                        self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, self._clock())
                        return response
                extracted_sha256 = hashlib.sha256(extracted.encode("utf-8")).hexdigest() if extracted is not None else None
                conn.execute(
                    "UPDATE chat_attachments SET status = 'ready', parse_state = 'ready', size = ?, sha256 = ?, extracted_text = ?, extracted_sha256 = ?, rejection_code = NULL, updated_at = ? WHERE id = ?",
                    (len(content), digest, extracted, extracted_sha256, self._clock(), attachment_id),
                )
                _append_event(conn, session_id, "attachment.updated", f"attachment:{attachment_id}:ready:{idempotency_key}", {"attachment_id": attachment_id, "status": "ready"}, self._clock())
                response = self._view(conn, owner_id, session_id, attachment_id)
                self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, self._clock())
                return response
        finally:
            if quarantine.exists() and not self._is_failed(owner_id, session_id, attachment_id):
                quarantine.unlink(missing_ok=True)

    def retry(self, owner_id: str, session_id: str, attachment_id: str, *, idempotency_key: str) -> JSON:
        owner_id, session_id, attachment_id = (_text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200), _text(attachment_id, "attachment_id", 200))
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request_hash = _hash({"attachment_id": attachment_id, "retry": True})
        content: bytes | None = None
        with self._database.connect() as conn:
            replay = conn.execute("SELECT request_hash, response_json FROM chat_attachment_operations WHERE owner_id = ? AND idempotency_key = ?", (owner_id, idempotency_key)).fetchone()
            if replay:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatAttachmentError("idempotency_conflict", "附件请求与已接受请求冲突")
                return json.loads(str(replay["response_json"]))
            row = self._owned(conn, owner_id, session_id, attachment_id)
            if row["status"] != "failed":
                raise ChatAttachmentError("attachment_not_retryable", "当前附件状态不能重试")
            quarantine = self._quarantine / attachment_id
            if quarantine.exists():
                content = quarantine.read_bytes()
            else:
                raise ChatAttachmentError("attachment_unavailable", "附件暂存内容不可用")
        assert content is not None
        upload_key = "retry-upload-" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        response = self.upload(owner_id, session_id, attachment_id, content, idempotency_key=upload_key)
        with self._database.connect() as conn:
            self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, self._clock())
        return response

    def bind(self, owner_id: str, session_id: str, message_id: str, attachment_ids: list[str]) -> list[JSON]:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bound = self.bind_in(conn, owner_id, session_id, message_id, attachment_ids)
            conn.commit()
            return bound

    def bind_in(self, conn: sqlite3.Connection, owner_id: str, session_id: str, message_id: str, attachment_ids: list[str]) -> list[JSON]:
        if (
            not isinstance(attachment_ids, list)
            or any(not isinstance(item, str) for item in attachment_ids)
            or len(attachment_ids) > MAX_MESSAGE_ATTACHMENTS
            or len(set(attachment_ids)) != len(attachment_ids)
        ):
            raise ChatAttachmentError("attachment_count_limit", "每条消息最多绑定 5 个附件")
        message = conn.execute("SELECT id FROM chat_messages WHERE id = ? AND session_id = ?", (message_id, session_id)).fetchone()
        if message is None:
            raise ChatAttachmentError("chat_message_not_found", "Chat message not found")
        rows = [self._owned(conn, owner_id, session_id, item) for item in attachment_ids]
        if any(row["status"] != "ready" for row in rows):
            raise ChatAttachmentError("attachment_not_ready", "只有已通过安全检查的附件可以发送")
        if sum(int(row["size"] or 0) for row in rows) > MAX_MESSAGE_BYTES:
            raise ChatAttachmentError("attachment_total_too_large", "单条消息附件总大小不能超过 50MB")
        for row in rows:
            if row["message_id"] is not None:
                raise ChatAttachmentError("attachment_bound", "已发送附件不能再次绑定")
            conn.execute("UPDATE chat_attachments SET message_id = ?, updated_at = ? WHERE id = ? AND message_id IS NULL", (message_id, self._clock(), row["id"]))
        return [self._view(conn, owner_id, session_id, str(row["id"])) for row in rows]

    def model_inputs(self, owner_id: str, session_id: str, message_ids: list[str]) -> list[JSON]:
        """Project bounded ready content; paths and credentials never cross this seam."""
        owner_id = _text(owner_id, "owner_id", 200)
        session_id = _text(session_id, "session_id", 200)
        if not isinstance(message_ids, list) or len(message_ids) > 200 or any(
            not isinstance(item, str) or not item.strip() for item in message_ids
        ):
            raise ChatAttachmentError("invalid_request", "message_ids is invalid")
        if not message_ids:
            return []
        with self._database.connect() as conn:
            self._session(conn, owner_id, session_id)
            placeholders = ",".join("?" for _ in message_ids)
            rows = conn.execute(
                f"SELECT * FROM chat_attachments WHERE owner_id = ? AND session_id = ? "
                f"AND message_id IN ({placeholders}) AND status = 'ready' ORDER BY created_at, id",
                (owner_id, session_id, *message_ids),
            ).fetchall()
            projected: list[JSON] = []
            for row in rows:
                item: JSON = {
                    "attachment_id": str(row["id"]), "message_id": str(row["message_id"]),
                    "filename": str(row["filename"]), "content_type": str(row["content_type"]),
                    "sha256": str(row["sha256"]), "parse_state": str(row["parse_state"]),
                    "extraction_sha256": str(row["extracted_sha256"] or ""), "untrusted": True,
                }
                if str(row["content_type"]).startswith("image/"):
                    try:
                        raw = (self._blobs / str(row["sha256"])).read_bytes()
                    except OSError as exc:
                        raise ChatAttachmentError("attachment_unavailable", "附件文件暂时不可用") from exc
                    if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != str(row["sha256"]):
                        raise ChatAttachmentError("attachment_unavailable", "附件文件校验失败")
                    item["image_base64"] = base64.b64encode(raw).decode("ascii")
                else:
                    extracted = row["extracted_text"]
                    if not isinstance(extracted, str) or len(extracted.encode("utf-8")) > MAX_EXTRACTED_BYTES:
                        raise ChatAttachmentError("parse_failed", "附件提取内容不可用")
                    item["extracted_text"] = extracted
                projected.append(item)
                conn.execute(
                    "UPDATE chat_attachments SET model_use_status = 'included', updated_at = ? WHERE id = ?",
                    (self._clock(), row["id"]),
                )
            return projected

    def contains_image(self, owner_id: str, session_id: str, attachment_ids: list[str]) -> bool:
        if not attachment_ids:
            return False
        with self._database.connect() as conn:
            rows = [
                self._owned(conn, _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200), _text(item, "attachment_id", 200))
                for item in attachment_ids
            ]
            return any(
                row["status"] == "ready"
                and row["message_id"] is None
                and str(row["content_type"]).startswith("image/")
                for row in rows
            )

    def branch_contains_image(self, owner_id: str, session_id: str, message_id: str | None = None) -> bool:
        with self._database.connect() as conn:
            owner_id, session_id = _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200)
            session = self._session(conn, owner_id, session_id)
            head_id = _text(message_id, "message_id", 200) if message_id is not None else conn.execute(
                "SELECT current_head_id FROM chat_sessions WHERE id = ?", (session["id"],),
            ).fetchone()[0]
            if not isinstance(head_id, str) or not head_id:
                return False
            row = conn.execute(
                """WITH RECURSIVE branch(id, parent_id) AS (
                       SELECT id, parent_id FROM chat_messages WHERE id = ? AND session_id = ?
                       UNION ALL
                       SELECT message.id, message.parent_id FROM chat_messages message
                       JOIN branch ON message.id = branch.parent_id
                       WHERE message.session_id = ?
                   )
                   SELECT 1 FROM chat_attachments attachment
                   WHERE attachment.owner_id = ? AND attachment.session_id = ?
                     AND attachment.status = 'ready' AND attachment.content_type LIKE 'image/%'
                     AND attachment.message_id IN (SELECT id FROM branch)
                   LIMIT 1""",
                (head_id, session_id, session_id, owner_id, session_id),
            ).fetchone()
            return row is not None

    def message_views(self, owner_id: str, session_id: str, message_ids: list[str]) -> list[JSON]:
        with self._database.connect() as conn:
            return self.message_views_in(conn, owner_id, session_id, message_ids)

    def message_views_in(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        message_ids: list[str],
    ) -> list[JSON]:
        if not message_ids:
            return []
        owner_id, session_id = _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200)
        self._session(conn, owner_id, session_id)
        placeholders = ",".join("?" for _ in message_ids)
        rows = conn.execute(
            f"SELECT id FROM chat_attachments WHERE owner_id = ? AND session_id = ? AND message_id IN ({placeholders}) ORDER BY created_at, id",
            (owner_id, session_id, *message_ids),
        ).fetchall()
        return [self._view(conn, owner_id, session_id, str(row["id"])) for row in rows]

    def list(self, owner_id: str, session_id: str) -> list[JSON]:
        with self._database.connect() as conn:
            self._session(conn, _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200))
            rows = conn.execute("SELECT id FROM chat_attachments WHERE owner_id = ? AND session_id = ? ORDER BY created_at, id", (owner_id, session_id)).fetchall()
            return [self._view(conn, owner_id, session_id, str(row["id"])) for row in rows]

    def get(self, owner_id: str, session_id: str, attachment_id: str) -> JSON:
        with self._database.connect() as conn:
            return self._view(conn, _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200), _text(attachment_id, "attachment_id", 200))

    def download(self, owner_id: str, session_id: str, attachment_id: str) -> tuple[bytes, JSON]:
        with self._database.connect() as conn:
            view = self._view(conn, _text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200), _text(attachment_id, "attachment_id", 200))
        if view["status"] != "ready":
            raise ChatAttachmentError("attachment_not_ready", "附件尚未准备好")
        path = self._blobs / str(view["sha256"])
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ChatAttachmentError("attachment_unavailable", "附件文件暂时不可用") from exc
        return content, view

    def delete(self, owner_id: str, session_id: str, attachment_id: str, *, idempotency_key: str) -> JSON:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner_id, session_id, attachment_id = (_text(owner_id, "owner_id", 200), _text(session_id, "session_id", 200), _text(attachment_id, "attachment_id", 200))
            idempotency_key = _text(idempotency_key, "idempotency_key", 200)
            request_hash = _hash({"attachment_id": attachment_id})
            replay = conn.execute("SELECT request_hash, response_json FROM chat_attachment_operations WHERE owner_id = ? AND idempotency_key = ?", (owner_id, idempotency_key)).fetchone()
            if replay:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatAttachmentError("idempotency_conflict", "附件请求与已接受请求冲突")
                return json.loads(str(replay["response_json"]))
            row = self._owned(conn, owner_id, session_id, attachment_id)
            if row["message_id"] is not None:
                raise ChatAttachmentError("attachment_bound", "已发送附件不能删除")
            response = {"attachment_id": attachment_id, "deleted": True}
            _append_event(conn, session_id, "attachment.deleted", f"attachment:{attachment_id}:deleted:{idempotency_key}", {"attachment_id": attachment_id}, self._clock())
            conn.execute("DELETE FROM chat_attachments WHERE id = ?", (attachment_id,))
            self._record_operation(conn, owner_id, attachment_id, idempotency_key, request_hash, response, self._clock())
        self.collect_garbage()
        return response

    def matching_session_ids(self, owner_id: str, query: str) -> set[str]:
        with self._database.connect() as conn:
            owner_id = _text(owner_id, "owner_id", 200)
            query = _text(query, "query", 200)
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            value = f"%{escaped}%"
            return {str(row[0]) for row in conn.execute("SELECT DISTINCT session_id FROM chat_attachments WHERE owner_id = ? AND status = 'ready' AND (filename LIKE ? ESCAPE '\\' OR extracted_text LIKE ? ESCAPE '\\')", (owner_id, value, value))}

    def collect_garbage(self) -> None:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT sha256 FROM chat_attachment_gc").fetchall()
            has_handoff_refs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_handoff_attachments'",
            ).fetchone() is not None
            for row in rows:
                digest = str(row["sha256"])
                refs = conn.execute("SELECT 1 FROM chat_attachments WHERE sha256 = ? LIMIT 1", (digest,)).fetchone()
                retained = conn.execute(
                    "SELECT 1 FROM chat_handoff_attachments WHERE sha256 = ? LIMIT 1", (digest,),
                ).fetchone() if has_handoff_refs else None
                if refs is None and retained is None:
                    (self._blobs / digest).unlink(missing_ok=True)
                    conn.execute("DELETE FROM chat_attachment_blobs WHERE sha256 = ?", (digest,))
                conn.execute("DELETE FROM chat_attachment_gc WHERE sha256 = ?", (digest,))
            active_quarantine = {str(row["id"]) for row in conn.execute("SELECT id FROM chat_attachments")}
        if self._quarantine.exists():
            for path in self._quarantine.iterdir():
                if path.is_file() and path.name not in active_quarantine:
                    path.unlink(missing_ok=True)

    def _session(self, conn: sqlite3.Connection, owner_id: str, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT id FROM chat_sessions WHERE id = ? AND owner_id = ?", (session_id, owner_id)).fetchone()
        if row is None:
            raise ChatAttachmentError("attachment_not_found", "附件不存在")
        return row

    def _owned(self, conn: sqlite3.Connection, owner_id: str, session_id: str, attachment_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM chat_attachments WHERE id = ? AND session_id = ? AND owner_id = ?", (attachment_id, session_id, owner_id)).fetchone()
        if row is None:
            raise ChatAttachmentError("attachment_not_found", "附件不存在")
        return row

    def _view(self, conn: sqlite3.Connection, owner_id: str, session_id: str, attachment_id: str) -> JSON:
        row = self._owned(conn, owner_id, session_id, attachment_id)
        return {
            "id": str(row["id"]), "session_id": str(row["session_id"]),
            "filename": str(row["filename"]), "content_type": str(row["content_type"]),
            "size": int(row["size"] or row["declared_size"]), "sha256": str(row["sha256"] or ""),
            "status": str(row["status"]), "parse_state": str(row["parse_state"]),
            "extraction_sha256": str(row["extracted_sha256"] or ""),
            "model_use_status": str(row["model_use_status"]),
            "rejection_code": str(row["rejection_code"]) if row["rejection_code"] else None,
            "message_id": str(row["message_id"]) if row["message_id"] else None,
            "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]),
        }

    def _set_state(self, conn: sqlite3.Connection, row: sqlite3.Row, status: str, code: str | None, now: float) -> JSON:
        parse_state = status if status in {"ready", "rejected", "failed"} else "pending"
        conn.execute("UPDATE chat_attachments SET status = ?, parse_state = ?, rejection_code = ?, updated_at = ? WHERE id = ?", (status, parse_state, code, now, row["id"]))
        _append_event(conn, str(row["session_id"]), "attachment.updated", f"attachment:{row['id']}:{status}:{now}", {"attachment_id": str(row["id"]), "status": status, "rejection_code": code}, now)
        return self._view(conn, str(row["owner_id"]), str(row["session_id"]), str(row["id"]))

    def _complete_failure(self, owner_id: str, session_id: str, attachment_id: str, key: str, request_hash: str, status: str, code: str, *, remove: bool = False) -> JSON:
        with self._database.connect() as conn:
            row = self._owned(conn, owner_id, session_id, attachment_id)
            response = self._set_state(conn, row, status, code, self._clock())
            self._record_operation(conn, owner_id, attachment_id, key, request_hash, response, self._clock())
        if remove:
            (self._quarantine / attachment_id).unlink(missing_ok=True)
        return response

    def _is_failed(self, owner_id: str, session_id: str, attachment_id: str) -> bool:
        with self._database.connect() as conn:
            row = self._owned(conn, owner_id, session_id, attachment_id)
            return row["status"] == "failed"

    @staticmethod
    def _record_operation(conn: sqlite3.Connection, owner_id: str, attachment_id: str, key: str, request_hash: str, response: JSON, now: float) -> None:
        conn.execute("INSERT OR REPLACE INTO chat_attachment_operations(owner_id, attachment_id, idempotency_key, request_hash, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (owner_id, attachment_id, key, request_hash, json.dumps(response, ensure_ascii=False, sort_keys=True), now))


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ChatAttachmentError("invalid_request", f"{field} 无效")
    return value.strip()


def _filename(value: object) -> str:
    raw = _text(value, "filename", 240).replace("\\", "/")
    normalized = unicodedata.normalize("NFKC", posixpath.basename(raw))
    normalized = "".join(char if char.isprintable() and char not in "/\\" else "_" for char in normalized).strip(" .")
    if not normalized or len(normalized) > 120:
        raise ChatAttachmentError("invalid_filename", "文件名无效")
    return normalized


def _content_type(value: object) -> str:
    normalized = _text(value, "content_type", 120).split(";", 1)[0].strip().lower()
    return normalized


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_and_extract(filename: str, content_type: str, content: bytes) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ChatAttachmentError("mime_mismatch", "文件真实格式与声明不一致")
    if suffix in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
        raise ChatAttachmentError("mime_mismatch", "文件真实格式与声明不一致")
    if suffix == ".webp" and not (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
        raise ChatAttachmentError("mime_mismatch", "文件真实格式与声明不一致")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise ChatAttachmentError("mime_mismatch", "文件真实格式与声明不一致")
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ChatAttachmentError("parse_limit", "图片像素数量超过限制")
        except ChatAttachmentError:
            raise
        except Exception as exc:
            raise ChatAttachmentError("parse_failed", "图片解析失败") from exc
        return None
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ChatAttachmentError("parse_failed", "加密 PDF 不受支持")
            if len(reader.pages) > 100:
                raise ChatAttachmentError("parse_limit", "PDF 页数超过限制")
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except ChatAttachmentError:
            raise
        except Exception as exc:
            raise ChatAttachmentError("parse_failed", "PDF 解析失败") from exc
        return _safe_text(text)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChatAttachmentError("parse_failed", "文本必须使用 UTF-8 编码") from exc
    if "\x00" in text:
        raise ChatAttachmentError("parse_failed", "文本内容无效")
    try:
        if suffix == ".json":
            json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            events = list(yaml.parse(text))
            if len(events) > 20_000 or any(type(event).__name__ == "AliasEvent" for event in events):
                raise ChatAttachmentError("parse_limit", "YAML 结构超过解析限制")
            yaml.safe_load(text)
        elif suffix == ".csv":
            if sum(1 for _ in csv.reader(io.StringIO(text))) > 10_000:
                raise ChatAttachmentError("parse_limit", "CSV 行数超过限制")
    except ChatAttachmentError:
        raise
    except Exception as exc:
        raise ChatAttachmentError("parse_failed", "文件结构解析失败") from exc
    return _safe_text(text)


def _safe_text(text: str) -> str:
    if contains_sensitive(text) or ("apiVersion:" in text and "clusters:" in text and "users:" in text):
        raise ChatAttachmentError("sensitive_content", "疑似凭据或 Secure Input，不允许上传")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_EXTRACTED_BYTES:
        raise ChatAttachmentError("parse_limit", "提取文本超过限制")
    return text


def _clamav_scan(content: bytes) -> bool:
    host = os.getenv("AIOPS_CHAT_SCANNER_HOST", "").strip()
    if not host:
        raise OSError("chat scanner is not configured")
    port = int(os.getenv("AIOPS_CHAT_SCANNER_PORT", "3310"))
    timeout = float(os.getenv("AIOPS_CHAT_SCANNER_TIMEOUT", "5"))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"zINSTREAM\0")
        for offset in range(0, len(content), 1024 * 1024):
            chunk = content[offset : offset + 1024 * 1024]
            sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
        sock.sendall(b"\x00\x00\x00\x00")
        response = sock.recv(1024).decode("utf-8", "replace").strip().upper()
    if response.endswith("OK"):
        return True
    if "FOUND" in response:
        return False
    raise OSError("invalid scanner response")


def _append_event(conn: sqlite3.Connection, session_id: str, event_type: str, key: str, payload: JSON, now: float) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO chat_events(session_id, type, idempotency_key, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, event_type, key, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
    )
