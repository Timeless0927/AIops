"""Gateway Notification Center and Feishu notification-only delivery."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib import error, request
from urllib.parse import quote


JSON = dict[str, Any]
T = TypeVar("T")

SUPPORTED_NOTIFICATION_TYPES = (
    "new_incident",
    "diagnosis_ready",
    "approval_required",
    "approval_result",
    "execution_result",
    "unowned_alert",
)

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50
_DISABLED_VALUES = {"", "0", "false", "no", "off"}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    incident_id TEXT,
    approval_id TEXT,
    service_id TEXT,
    team_id TEXT,
    platform TEXT NOT NULL,
    receive_id_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    delivery_status TEXT NOT NULL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at REAL,
    last_delivery_error TEXT,
    last_delivery_at REAL,
    target_message_id TEXT,
    payload_json TEXT NOT NULL,
    card_json TEXT NOT NULL,
    suppressed_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sent_at REAL
);

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
ON notification_deliveries(delivery_status, next_retry_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_type
ON notification_deliveries(notification_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_incident
ON notification_deliveries(incident_id, created_at DESC);
"""


@dataclass(frozen=True)
class NotificationSettings:
    """Runtime settings for Gateway-owned notifications."""

    console_base_url: str
    max_attempts: int
    retry_delay_seconds: float
    channel_config: JSON
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "NotificationSettings":
        return cls(
            console_base_url=os.getenv("AIOPS_CONSOLE_BASE_URL", "http://aiops-console.local").rstrip("/"),
            max_attempts=_int_env("AIOPS_NOTIFICATION_MAX_ATTEMPTS", 3),
            retry_delay_seconds=_float_env("AIOPS_NOTIFICATION_RETRY_DELAY_SECONDS", 60.0),
            channel_config=_load_channel_config(),
            dry_run=os.getenv("AIOPS_NOTIFICATION_DRY_RUN", "").strip().lower() not in _DISABLED_VALUES,
        )


@dataclass(frozen=True)
class NotificationTarget:
    """Resolved notification target."""

    platform: str
    receive_id_type: str
    chat_id: str
    team_id: str | None
    service_id: str | None
    reason: str

    def to_dict(self) -> JSON:
        return {
            "platform": self.platform,
            "receive_id_type": self.receive_id_type,
            "chat_id": self.chat_id,
            "team_id": self.team_id,
            "service_id": self.service_id,
            "reason": self.reason,
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "notification_deliveries.db"
    return _project_root() / "data" / "notification_deliveries.db"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _load_channel_config() -> JSON:
    raw = os.getenv("AIOPS_NOTIFICATION_CHANNELS_JSON", "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, dict):
            return decoded

    default_chat_id = (
        os.getenv("AIOPS_DEFAULT_FEISHU_CHAT_ID")
        or os.getenv("FEISHU_MAIN_CHAT_ID")
        or os.getenv("FEISHU_ALERT_CHAT_ID")
        or ""
    ).strip()
    if not default_chat_id:
        return {}
    return {
        "default_team_id": "default",
        "teams": {
            "default": {
                "name": "default",
                "feishu_chat_id": default_chat_id,
            }
        },
    }


def normalize_notification_payload(payload: JSON) -> JSON:
    """Validate and normalize a notification request."""

    notification_type = str(payload.get("notification_type") or payload.get("type") or "").strip()
    if notification_type not in SUPPORTED_NOTIFICATION_TYPES:
        raise ValueError(
            "notification_type must be one of: " + ", ".join(SUPPORTED_NOTIFICATION_TYPES)
        )

    context = payload.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("context must be a JSON object")

    normalized = dict(payload)
    normalized["notification_type"] = notification_type
    normalized["context"] = dict(context)
    normalized.setdefault("notification_id", f"ntf-{uuid.uuid4().hex}")
    return normalized


def resolve_notification_target(payload: JSON, settings: NotificationSettings) -> NotificationTarget | None:
    """Resolve service/team ownership into a Feishu chat target."""

    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    direct_chat = _first_text(
        payload.get("feishu_chat_id"),
        payload.get("chat_id"),
        context.get("feishu_chat_id"),
        context.get("chat_id"),
    )
    service_id = _first_text(
        payload.get("service_id"),
        context.get("service_id"),
        context.get("service_name"),
        context.get("service"),
    )
    team_id = _first_text(
        payload.get("team_id"),
        context.get("team_id"),
        context.get("owner_team"),
        context.get("team"),
    )
    if direct_chat:
        return NotificationTarget(
            platform="feishu",
            receive_id_type="chat_id",
            chat_id=direct_chat,
            team_id=team_id,
            service_id=service_id,
            reason="direct_chat_override",
        )

    config = settings.channel_config
    services = config.get("services") if isinstance(config.get("services"), dict) else {}
    teams = config.get("teams") if isinstance(config.get("teams"), dict) else {}

    if service_id and isinstance(services.get(service_id), dict):
        service_cfg = services[service_id]
        configured_team = _first_text(service_cfg.get("team_id"), service_cfg.get("owner_team"))
        if configured_team:
            team_id = configured_team
        service_chat = _first_text(service_cfg.get("feishu_chat_id"), service_cfg.get("chat_id"))
        if service_chat:
            return NotificationTarget(
                platform="feishu",
                receive_id_type="chat_id",
                chat_id=service_chat,
                team_id=team_id,
                service_id=service_id,
                reason="service_channel",
            )

    if team_id and isinstance(teams.get(team_id), dict):
        team_chat = _first_text(teams[team_id].get("feishu_chat_id"), teams[team_id].get("chat_id"))
        if team_chat:
            return NotificationTarget(
                platform="feishu",
                receive_id_type="chat_id",
                chat_id=team_chat,
                team_id=team_id,
                service_id=service_id,
                reason="team_channel",
            )

    default_team_id = _first_text(config.get("default_team_id"), "default")
    default_team = teams.get(default_team_id) if isinstance(teams.get(default_team_id), dict) else {}
    default_chat = _first_text(
        default_team.get("feishu_chat_id") if isinstance(default_team, dict) else None,
        default_team.get("chat_id") if isinstance(default_team, dict) else None,
        config.get("default_feishu_chat_id"),
        config.get("default_chat_id"),
    )
    if default_chat:
        return NotificationTarget(
            platform="feishu",
            receive_id_type="chat_id",
            chat_id=default_chat,
            team_id=team_id or default_team_id,
            service_id=service_id,
            reason="default_team_channel",
        )
    return None


def build_notification_card(payload: JSON, settings: NotificationSettings) -> JSON:
    """Build a Feishu interactive card with URL-only actions."""

    notification_type = str(payload["notification_type"])
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    template = _template_for(notification_type, context)
    incident_id = _first_text(payload.get("incident_id"), context.get("incident_id"))
    approval_id = _first_text(payload.get("approval_id"), context.get("approval_id"))
    service_id = _first_text(payload.get("service_id"), context.get("service_id"), context.get("service_name"))
    team_id = _first_text(payload.get("team_id"), context.get("team_id"), context.get("owner_team"))
    summary = _first_text(payload.get("summary"), context.get("summary"), context.get("description"))
    console_url = _console_url(payload, settings)

    fields = [
        ("Incident", incident_id),
        ("服务", service_id),
        ("团队", team_id),
        ("严重级别", context.get("severity")),
        ("状态", context.get("status") or payload.get("status")),
        ("风险", context.get("risk_level") or payload.get("risk_level")),
        ("审批", approval_id),
    ]
    field_text = "\n".join(
        f"**{label}:** {value}" for label, value in fields if value is not None and str(value).strip()
    )
    content_parts = [template["body"]]
    if summary:
        content_parts.append(f"**摘要:** {summary}")
    if field_text:
        content_parts.append(field_text)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template["header_template"],
            "title": {"tag": "plain_text", "content": template["title"]},
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(content_parts)},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": template["button_text"]},
                        "type": "primary",
                        "url": console_url,
                    }
                ],
            },
        ],
    }
    return card


def notification_template_example(notification_type: str, settings: NotificationSettings | None = None) -> JSON:
    """Return a sanitized template example for comments, docs, and tests."""

    effective_settings = settings or NotificationSettings.from_env()
    payload = {
        "notification_type": notification_type,
        "incident_id": "inc_example",
        "approval_id": "ap_example" if notification_type in {"approval_required", "approval_result"} else None,
        "summary": "示例通知摘要",
        "context": {
            "service_id": "checkout-api",
            "owner_team": "sre",
            "severity": "critical",
            "risk_level": "high",
            "status": "pending",
        },
    }
    return build_notification_card(normalize_notification_payload(payload), effective_settings)


class NotificationDeliveryDB:
    """SQLite-backed notification delivery store with retry state."""

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
        self._conn.executescript(_SCHEMA_SQL)

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
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_row(dict(row)) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return _decode_row(dict(row)) if row is not None else None

    def upsert_pending(self, delivery: JSON) -> JSON:
        now = time.time()
        delivery_id = str(delivery.get("id") or uuid.uuid4())

        def _write(conn: sqlite3.Connection) -> str:
            existing = conn.execute(
                "SELECT id, delivery_status FROM notification_deliveries WHERE dedupe_key = ?",
                (delivery["dedupe_key"],),
            ).fetchone()
            if existing is not None:
                if str(existing["delivery_status"]) in {"sent", "suppressed"}:
                    return str(existing["id"])
                conn.execute(
                    """
                    UPDATE notification_deliveries
                    SET notification_id = ?,
                        notification_type = ?,
                        incident_id = ?,
                        approval_id = ?,
                        service_id = ?,
                        team_id = ?,
                        platform = ?,
                        receive_id_type = ?,
                        chat_id = ?,
                        template_id = ?,
                        payload_hash = ?,
                        max_attempts = ?,
                        payload_json = ?,
                        card_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        delivery["notification_id"],
                        delivery["notification_type"],
                        delivery.get("incident_id"),
                        delivery.get("approval_id"),
                        delivery.get("service_id"),
                        delivery.get("team_id"),
                        delivery["platform"],
                        delivery["receive_id_type"],
                        delivery["chat_id"],
                        delivery["template_id"],
                        delivery["payload_hash"],
                        delivery["max_attempts"],
                        delivery["payload_json"],
                        delivery["card_json"],
                        now,
                        existing["id"],
                    ),
                )
                return str(existing["id"])

            conn.execute(
                """
                INSERT INTO notification_deliveries (
                    id, notification_id, notification_type, incident_id, approval_id,
                    service_id, team_id, platform, receive_id_type, chat_id, template_id,
                    payload_hash, dedupe_key, delivery_status, delivery_attempts,
                    max_attempts, next_retry_at, last_delivery_error, last_delivery_at,
                    target_message_id, payload_json, card_json, suppressed_reason,
                    created_at, updated_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL,
                    NULL, NULL, NULL, ?, ?, NULL, ?, ?, NULL)
                """,
                (
                    delivery_id,
                    delivery["notification_id"],
                    delivery["notification_type"],
                    delivery.get("incident_id"),
                    delivery.get("approval_id"),
                    delivery.get("service_id"),
                    delivery.get("team_id"),
                    delivery["platform"],
                    delivery["receive_id_type"],
                    delivery["chat_id"],
                    delivery["template_id"],
                    delivery["payload_hash"],
                    delivery["dedupe_key"],
                    delivery["max_attempts"],
                    delivery["payload_json"],
                    delivery["card_json"],
                    now,
                    now,
                ),
            )
            return delivery_id

        result_id = self._execute_write(_write)
        row = self.get_delivery(result_id)
        if row is None:
            raise ValueError(f"notification delivery not found after upsert: {result_id}")
        return row

    def get_delivery(self, delivery_id: str) -> JSON | None:
        return self._fetchone("SELECT * FROM notification_deliveries WHERE id = ?", (delivery_id,))

    def list_deliveries(
        self,
        *,
        status: str | None = None,
        notification_type: str | None = None,
        limit: int = 100,
    ) -> list[JSON]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("delivery_status = ?")
            params.append(status)
        if notification_type:
            clauses.append("notification_type = ?")
            params.append(notification_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        return self._fetchall(
            f"SELECT * FROM notification_deliveries{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

    def list_due_retries(self, *, limit: int = 100, now: float | None = None) -> list[JSON]:
        effective_now = now if now is not None else time.time()
        return self._fetchall(
            """
            SELECT *
            FROM notification_deliveries
            WHERE delivery_status = 'failed'
              AND delivery_attempts < max_attempts
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (effective_now, max(1, min(limit, 500))),
        )

    def mark_suppressed(self, delivery_id: str, reason: str) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE notification_deliveries
                SET delivery_status = 'suppressed',
                    suppressed_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, delivery_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"notification delivery not found: {delivery_id}")

        self._execute_write(_write)
        row = self.get_delivery(delivery_id)
        if row is None:
            raise ValueError(f"notification delivery not found: {delivery_id}")
        return row

    def mark_sent(self, delivery_id: str, message_id: str) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE notification_deliveries
                SET delivery_status = 'sent',
                    target_message_id = ?,
                    last_delivery_error = NULL,
                    next_retry_at = NULL,
                    last_delivery_at = ?,
                    sent_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (message_id, now, now, now, delivery_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"notification delivery not found: {delivery_id}")

        self._execute_write(_write)
        row = self.get_delivery(delivery_id)
        if row is None:
            raise ValueError(f"notification delivery not found: {delivery_id}")
        return row

    def mark_failed(
        self,
        delivery_id: str,
        error_message: str,
        *,
        retry_delay_seconds: float,
        retryable: bool = True,
    ) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT delivery_attempts, max_attempts FROM notification_deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"notification delivery not found: {delivery_id}")
            attempts = int(row["delivery_attempts"]) + 1
            max_attempts = int(row["max_attempts"])
            status = "dead_letter" if not retryable or attempts >= max_attempts else "failed"
            next_retry_at = None if status == "dead_letter" else now + retry_delay_seconds
            conn.execute(
                """
                UPDATE notification_deliveries
                SET delivery_status = ?,
                    delivery_attempts = ?,
                    last_delivery_error = ?,
                    last_delivery_at = ?,
                    next_retry_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, attempts, error_message, now, next_retry_at, now, delivery_id),
            )

        self._execute_write(_write)
        row = self.get_delivery(delivery_id)
        if row is None:
            raise ValueError(f"notification delivery not found: {delivery_id}")
        return row


class FeishuNotificationChannel:
    """Feishu notification-only channel.

    The runtime prefers the official lark-oapi SDK when available. A small
    OpenAPI fallback remains for smoke images and tests where the SDK is absent.
    """

    def __init__(self, settings: NotificationSettings) -> None:
        self.settings = settings
        self._token_cache: tuple[str, float] | None = None

    def send_card(self, target: NotificationTarget, card: JSON) -> JSON:
        if self.settings.dry_run:
            digest = stable_hash({"target": target.to_dict(), "card": card})[:16]
            return {"ok": True, "message_id": f"dry-run-{digest}", "dry_run": True}
        if not target.chat_id:
            return {"ok": False, "retryable": False, "error": "feishu chat_id is empty"}

        sdk_result = self._send_with_sdk(target, card)
        if sdk_result is not None:
            return sdk_result
        return self._send_with_openapi(target, card)

    def _send_with_sdk(self, target: NotificationTarget, card: JSON) -> JSON | None:
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            return None
        try:
            import lark_oapi as lark  # type: ignore
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody  # type: ignore
        except ImportError:
            return None

        try:
            client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(target.chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(target.receive_id_type)
                .request_body(body)
                .build()
            )
            response = client.im.v1.message.create(req)
            if not response.success():
                return {
                    "ok": False,
                    "retryable": _is_retryable_error_code(getattr(response, "code", None)),
                    "error": getattr(response, "msg", "") or "feishu sdk send failed",
                    "code": getattr(response, "code", None),
                }
            data = getattr(response, "data", None)
            message_id = _first_text(
                getattr(data, "message_id", None),
                getattr(data, "messageId", None),
                getattr(data, "open_message_id", None),
            )
            return {"ok": True, "message_id": message_id or f"feishu-{uuid.uuid4().hex}"}
        except Exception as exc:
            return {"ok": False, "retryable": True, "error": str(exc)}

    def _send_with_openapi(self, target: NotificationTarget, card: JSON) -> JSON:
        token = os.getenv("FEISHU_TENANT_ACCESS_TOKEN", "").strip() or self._tenant_access_token()
        if not token:
            return {"ok": False, "retryable": False, "error": "feishu credentials are not configured"}

        api = (
            "https://open.feishu.cn/open-apis/im/v1/messages"
            f"?receive_id_type={target.receive_id_type}"
        )
        payload = {
            "receive_id": target.chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        return _post_feishu_json(api, payload, token)

    def _tenant_access_token(self) -> str | None:
        now = time.time()
        if self._token_cache is not None and self._token_cache[1] > now:
            return self._token_cache[0]
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            return None
        payload = {"app_id": app_id, "app_secret": app_secret}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                decoded = json.loads(response.read().decode("utf-8") or "{}")
        except (OSError, TimeoutError, error.URLError, json.JSONDecodeError):
            return None
        token = _first_text(decoded.get("tenant_access_token"))
        if not token:
            return None
        try:
            expire_seconds = int(decoded.get("expire", 7200))
        except (TypeError, ValueError):
            expire_seconds = 7200
        self._token_cache = (token, now + max(60, expire_seconds - 300))
        return token


class NotificationCenter:
    """Gateway control-plane notification service."""

    def __init__(
        self,
        db: NotificationDeliveryDB | None = None,
        channel: FeishuNotificationChannel | None = None,
        settings: NotificationSettings | None = None,
    ) -> None:
        self.settings = settings or NotificationSettings.from_env()
        self.db = db or NotificationDeliveryDB()
        self.channel = channel or FeishuNotificationChannel(self.settings)

    def send_notification(self, payload: JSON) -> JSON:
        normalized = normalize_notification_payload(payload)
        target = resolve_notification_target(normalized, self.settings)
        if target is None:
            target = NotificationTarget(
                platform="feishu",
                receive_id_type="chat_id",
                chat_id="",
                team_id=None,
                service_id=_context_text(normalized, "service_id", "service_name", "service"),
                reason="channel_not_configured",
            )
        card = build_notification_card(normalized, self.settings)
        delivery = _delivery_payload(normalized, target, card, self.settings)
        row = self.db.upsert_pending(delivery)

        if row["delivery_status"] in {"sent", "suppressed"}:
            return {
                "ok": row["delivery_status"] == "sent",
                "idempotent": True,
                "delivery": row,
                "target": target.to_dict(),
                "card": card,
            }
        if _is_suppressed(normalized):
            reason = str(normalized.get("suppress_reason") or normalized.get("suppressed_reason") or "suppressed")
            row = self.db.mark_suppressed(row["id"], reason)
            return {"ok": True, "suppressed": True, "delivery": row, "target": target.to_dict(), "card": card}
        if not target.chat_id:
            row = self.db.mark_failed(
                row["id"],
                "feishu channel is not configured for service/team/default",
                retry_delay_seconds=self.settings.retry_delay_seconds,
                retryable=False,
            )
            return {"ok": False, "delivery": row, "target": target.to_dict(), "card": card}

        row = self._attempt_send(row, target, card)
        return {
            "ok": row["delivery_status"] == "sent",
            "delivery": row,
            "target": target.to_dict(),
            "card": card,
        }

    def retry_due_deliveries(self, *, limit: int = 100) -> JSON:
        due = self.db.list_due_retries(limit=limit)
        results = [self.retry_delivery(str(row["id"])) for row in due]
        return {"ok": True, "retried": len(results), "deliveries": results}

    def retry_delivery(self, delivery_id: str) -> JSON:
        row = self.db.get_delivery(delivery_id)
        if row is None:
            raise ValueError(f"notification delivery not found: {delivery_id}")
        if row["delivery_status"] not in {"failed", "pending"}:
            return {"ok": row["delivery_status"] == "sent", "skipped": True, "delivery": row}
        target = NotificationTarget(
            platform=str(row["platform"]),
            receive_id_type=str(row["receive_id_type"]),
            chat_id=str(row["chat_id"]),
            team_id=row.get("team_id"),
            service_id=row.get("service_id"),
            reason="stored_delivery_retry",
        )
        card = row["card"]
        updated = self._attempt_send(row, target, card)
        return {"ok": updated["delivery_status"] == "sent", "delivery": updated}

    def list_deliveries(
        self,
        *,
        status: str | None = None,
        notification_type: str | None = None,
        limit: int = 100,
    ) -> list[JSON]:
        return self.db.list_deliveries(status=status, notification_type=notification_type, limit=limit)

    def _attempt_send(self, row: JSON, target: NotificationTarget, card: JSON) -> JSON:
        response = self.channel.send_card(target, card)
        if response.get("ok"):
            message_id = str(response.get("message_id") or f"feishu-{uuid.uuid4().hex}")
            return self.db.mark_sent(str(row["id"]), message_id)
        return self.db.mark_failed(
            str(row["id"]),
            str(response.get("error") or "feishu notification send failed"),
            retry_delay_seconds=self.settings.retry_delay_seconds,
            retryable=bool(response.get("retryable", True)),
        )


def get_center() -> NotificationCenter:
    global _CENTER
    if _CENTER is None:
        _CENTER = NotificationCenter()
    return _CENTER


def send_notification(payload: JSON) -> JSON:
    return get_center().send_notification(payload)


def list_deliveries(
    *,
    status: str | None = None,
    notification_type: str | None = None,
    limit: int = 100,
) -> list[JSON]:
    return get_center().list_deliveries(status=status, notification_type=notification_type, limit=limit)


def retry_due_deliveries(*, limit: int = 100) -> JSON:
    return get_center().retry_due_deliveries(limit=limit)


def retry_delivery(delivery_id: str) -> JSON:
    return get_center().retry_delivery(delivery_id)


def handle_send_http_request(payload: JSON) -> tuple[int, JSON]:
    try:
        result = send_notification(payload)
    except ValueError as exc:
        return 400, {"ok": False, "message": str(exc)}
    return 202 if isinstance(result.get("delivery"), dict) else 503, result


def handle_retry_http_request(payload: JSON) -> tuple[int, JSON]:
    try:
        delivery_id = str(payload.get("delivery_id") or "").strip()
        if delivery_id:
            return 200, retry_delivery(delivery_id)
        limit = _coerce_limit(payload.get("limit"), default=100)
        return 200, retry_due_deliveries(limit=limit)
    except ValueError as exc:
        return 404, {"ok": False, "message": str(exc)}


def template_catalog(settings: NotificationSettings | None = None) -> JSON:
    effective_settings = settings or NotificationSettings.from_env()
    return {
        "notification_types": list(SUPPORTED_NOTIFICATION_TYPES),
        "templates": {
            notification_type: notification_template_example(notification_type, effective_settings)
            for notification_type in SUPPORTED_NOTIFICATION_TYPES
        },
    }


def stable_hash(payload: JSON) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _delivery_payload(
    payload: JSON,
    target: NotificationTarget,
    card: JSON,
    settings: NotificationSettings,
) -> JSON:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    incident_id = _first_text(payload.get("incident_id"), context.get("incident_id"))
    approval_id = _first_text(payload.get("approval_id"), context.get("approval_id"))
    payload_hash = stable_hash({"payload": payload, "card": card})
    dedupe_key = _first_text(payload.get("dedupe_key"), payload.get("idempotency_key"))
    if not dedupe_key:
        dedupe_key = "|".join(
            [
                str(payload["notification_type"]),
                str(incident_id or ""),
                str(approval_id or ""),
                str(_first_text(context.get("action_proposal_id"), payload.get("action_proposal_id")) or ""),
                payload_hash,
            ]
        )
    return {
        "notification_id": str(payload["notification_id"]),
        "notification_type": str(payload["notification_type"]),
        "incident_id": incident_id,
        "approval_id": approval_id,
        "service_id": target.service_id,
        "team_id": target.team_id,
        "platform": target.platform,
        "receive_id_type": target.receive_id_type,
        "chat_id": target.chat_id,
        "template_id": f"feishu.{payload['notification_type']}.v1",
        "payload_hash": payload_hash,
        "dedupe_key": dedupe_key,
        "max_attempts": settings.max_attempts,
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "card_json": json.dumps(card, ensure_ascii=False, sort_keys=True),
    }


def _template_for(notification_type: str, context: JSON) -> JSON:
    titles = {
        "new_incident": ("AIOps 新 Incident", "red", "打开 Incident"),
        "diagnosis_ready": ("AIOps 诊断已生成", "blue", "查看诊断"),
        "approval_required": ("AIOps 待审批提醒", "orange", "打开 Approval Center"),
        "approval_result": ("AIOps 审批结果", "green", "查看审批详情"),
        "execution_result": ("AIOps 执行结果", "wathet", "查看执行记录"),
        "unowned_alert": ("AIOps 未归属告警", "yellow", "维护服务归属"),
    }
    title, header_template, button_text = titles[notification_type]
    body = {
        "new_incident": "Gateway 已接收告警并创建 Incident。",
        "diagnosis_ready": "诊断结果已生成，请在内部 Console 查看证据和建议动作。",
        "approval_required": "有操作建议等待审批。飞书只负责通知，审批必须在内部系统完成。",
        "approval_result": f"审批状态已更新为 `{context.get('status') or 'unknown'}`。",
        "execution_result": f"执行状态已更新为 `{context.get('status') or 'unknown'}`。",
        "unowned_alert": "告警未匹配到明确服务归属，已投递到默认团队渠道。",
    }[notification_type]
    return {
        "title": title,
        "header_template": header_template,
        "button_text": button_text,
        "body": body,
    }


def _console_url(payload: JSON, settings: NotificationSettings) -> str:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    notification_type = str(payload["notification_type"])
    incident_id = _first_text(payload.get("incident_id"), context.get("incident_id"))
    approval_id = _first_text(payload.get("approval_id"), context.get("approval_id"))
    if notification_type in {"approval_required", "approval_result"} and approval_id:
        return f"{settings.console_base_url}/approval-center/{_url_path_component(approval_id)}"
    if notification_type == "unowned_alert":
        return f"{settings.console_base_url}/settings/service-ownership"
    if incident_id:
        return f"{settings.console_base_url}/incidents/{_url_path_component(incident_id)}"
    return settings.console_base_url


def _url_path_component(value: str) -> str:
    return quote(value.strip(), safe="")


def _context_text(payload: JSON, *keys: str) -> str | None:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    values: list[Any] = []
    for key in keys:
        values.append(payload.get(key))
        values.append(context.get(key))
    return _first_text(*values)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_suppressed(payload: JSON) -> bool:
    return bool(payload.get("suppress") or payload.get("suppressed") or payload.get("silent"))


def _decode_row(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["payload"] = _json_obj(decoded.pop("payload_json", "{}"))
    decoded["card"] = _json_obj(decoded.pop("card_json", "{}"))
    return decoded


def _json_obj(raw: Any) -> JSON:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _coerce_limit(value: Any, *, default: int) -> int:
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError):
        return default


def _is_retryable_error_code(code: Any) -> bool:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return True
    return value == 429 or 500 <= value < 600


def _post_feishu_json(api: str, payload: JSON, token: str) -> JSON:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        api,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw or "{}")
    except error.HTTPError as exc:
        return {"ok": False, "retryable": exc.code == 429 or exc.code >= 500, "error": str(exc), "code": exc.code}
    except (OSError, TimeoutError, error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "retryable": True, "error": str(exc)}

    code = decoded.get("code")
    if code not in (0, None):
        return {
            "ok": False,
            "retryable": _is_retryable_error_code(code),
            "error": decoded.get("msg") or "feishu api returned non-zero code",
            "code": code,
        }
    data = decoded.get("data") if isinstance(decoded.get("data"), dict) else decoded
    body_data = _json_obj(data.get("body")) if isinstance(data, dict) else {}
    message_id = _first_text(
        data.get("message_id") if isinstance(data, dict) else None,
        data.get("open_message_id") if isinstance(data, dict) else None,
        body_data.get("message_id"),
        body_data.get("open_message_id"),
    )
    return {"ok": True, "message_id": message_id or f"feishu-{uuid.uuid4().hex}"}


_CENTER: NotificationCenter | None = None
