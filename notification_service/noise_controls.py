"""Destination noise policy and bounded Notification Silences."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .notification_matching import matches, validate_match


JSON = dict[str, object]
MAX_SILENCE_SECONDS = 30 * 86400
DEFAULT_POLICY: JSON = {
    "timezone": "UTC",
    "quiet_hours": None,
    "hourly_limit": None,
    "digest_interval_seconds": None,
}


class NotificationNoiseControlError(ValueError):
    pass


class NotificationNoiseControls:
    def __init__(self, db_path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self._clock = clock

    def get_destination(self, destination_id: str) -> JSON:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone() is None:
                raise NotificationNoiseControlError("destination not found")
            row = conn.execute(
                "SELECT * FROM notification_destination_noise_controls WHERE destination_id = ?",
                (destination_id,),
            ).fetchone()
        return dict(DEFAULT_POLICY) if row is None else _policy_view(row)

    def update_destination(self, destination_id: str, payload: JSON) -> JSON:
        allowed = set(DEFAULT_POLICY)
        if not payload or set(payload) - allowed:
            raise NotificationNoiseControlError("unsupported destination noise control field")
        current = self.get_destination(destination_id)
        policy = _validate_policy({**current, **payload})
        quiet = policy["quiet_hours"]
        now = self._clock()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO notification_destination_noise_controls
                   (destination_id, timezone, quiet_start, quiet_end, hourly_limit, digest_interval_seconds, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(destination_id) DO UPDATE SET timezone=excluded.timezone,
                   quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end,
                   hourly_limit=excluded.hourly_limit, digest_interval_seconds=excluded.digest_interval_seconds,
                   updated_at=excluded.updated_at""",
                (
                    destination_id,
                    policy["timezone"],
                    quiet["start"] if isinstance(quiet, dict) else None,
                    quiet["end"] if isinstance(quiet, dict) else None,
                    policy["hourly_limit"],
                    policy["digest_interval_seconds"],
                    now,
                ),
            )
        return policy

    def create_silence(self, payload: JSON) -> JSON:
        if set(payload) != {"match", "reason", "expires_at"}:
            raise NotificationNoiseControlError("silence requires match, reason, and expires_at")
        try:
            match = validate_match(payload["match"], owner="silence")
        except ValueError as exc:
            raise NotificationNoiseControlError(str(exc)) from exc
        if not match:
            raise NotificationNoiseControlError("silence match must not be empty")
        reason = str(payload["reason"]).strip()
        expires_at = payload["expires_at"]
        now = self._clock()
        if not reason or len(reason) > 500:
            raise NotificationNoiseControlError("silence reason is required")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool) or not now < expires_at <= now + MAX_SILENCE_SECONDS:
            raise NotificationNoiseControlError("silence expiry must be within 30 days")
        silence_id = f"silence:{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notification_silences VALUES (?, ?, ?, ?, ?)",
                (silence_id, _json(match), reason, float(expires_at), now),
            )
        return self.get_silence(silence_id)

    def get_silence(self, silence_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_silences WHERE id = ?", (silence_id,)).fetchone()
        if row is None:
            raise NotificationNoiseControlError("silence not found")
        return _silence_view(row, self._clock())

    def list_silences(self) -> list[JSON]:
        now = self._clock()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_silences ORDER BY created_at DESC, id").fetchall()
        return [_silence_view(row, now) for row in rows]

    def evaluate(
        self,
        destination_id: str,
        request: JSON,
        hourly_count: int = 0,
        oldest_delivery_at: float | None = None,
    ) -> JSON:
        now = self._clock()
        with self._connect() as conn:
            silences = conn.execute(
                "SELECT * FROM notification_silences WHERE expires_at > ? ORDER BY created_at DESC, id",
                (now,),
            ).fetchall()
        for row in silences:
            if matches(json.loads(str(row["match_json"])), request):
                return {
                    "result": "silence",
                    "next_attempt_at": None,
                    "reason": str(row["reason"]),
                    "silence_id": str(row["id"]),
                }
        if request["severity"] == "critical":
            return {"result": "immediate", "next_attempt_at": now, "reason": None}
        policy = self.get_destination(destination_id)
        quiet_end = _quiet_end(now, policy)
        if quiet_end is not None:
            timezone = ZoneInfo(str(policy["timezone"]))
            label = datetime.fromtimestamp(quiet_end, timezone).isoformat()
            return {"result": "quiet_hours", "next_attempt_at": quiet_end, "reason": f"quiet hours until {label}"}
        limit = policy["hourly_limit"]
        if isinstance(limit, int) and hourly_count >= limit and oldest_delivery_at is not None:
            return {"result": "hourly_limit", "next_attempt_at": oldest_delivery_at + 3600, "reason": f"hourly limit {limit} reached"}
        interval = policy["digest_interval_seconds"]
        if isinstance(interval, int):
            return {
                "result": "digest",
                "next_attempt_at": (int(now) // interval + 1) * interval,
                "reason": f"digest interval {interval} seconds",
            }
        return {"result": "immediate", "next_attempt_at": now, "reason": None}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _validate_policy(value: JSON) -> JSON:
    timezone = value["timezone"]
    if not isinstance(timezone, str):
        raise NotificationNoiseControlError("timezone must be an IANA timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise NotificationNoiseControlError("timezone must be an IANA timezone") from exc
    quiet = value["quiet_hours"]
    if quiet is not None:
        if not isinstance(quiet, dict) or set(quiet) != {"start", "end"}:
            raise NotificationNoiseControlError("quiet_hours requires start and end")
        start = _clock_minutes(quiet["start"])
        end = _clock_minutes(quiet["end"])
        if start == end:
            raise NotificationNoiseControlError("quiet_hours start and end must differ")
    for field, maximum in (("hourly_limit", 10_000), ("digest_interval_seconds", 86400)):
        item = value[field]
        minimum = 60 if field == "digest_interval_seconds" else 1
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum):
            raise NotificationNoiseControlError(f"{field} must be between {minimum} and {maximum}")
    return {key: value[key] for key in DEFAULT_POLICY}


def _quiet_end(now: float, policy: JSON) -> float | None:
    quiet = policy["quiet_hours"]
    if not isinstance(quiet, dict):
        return None
    timezone = ZoneInfo(str(policy["timezone"]))
    local = datetime.fromtimestamp(now, timezone)
    start = _clock_minutes(quiet["start"])
    end = _clock_minutes(quiet["end"])
    minute = local.hour * 60 + local.minute
    active = start <= minute < end if start < end else minute >= start or minute < end
    if not active:
        return None
    end_day = local.date() + timedelta(days=1 if start > end and minute >= start else 0)
    return datetime.combine(end_day, datetime.min.time(), timezone).replace(hour=end // 60, minute=end % 60).timestamp()


def _clock_minutes(value: object) -> int:
    if not isinstance(value, str):
        raise NotificationNoiseControlError("quiet hour must use HH:MM")
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise NotificationNoiseControlError("quiet hour must use HH:MM") from exc
    return parsed.hour * 60 + parsed.minute


def _policy_view(row: sqlite3.Row) -> JSON:
    quiet = None if row["quiet_start"] is None else {"start": str(row["quiet_start"]), "end": str(row["quiet_end"])}
    return {"timezone": str(row["timezone"]), "quiet_hours": quiet, "hourly_limit": row["hourly_limit"], "digest_interval_seconds": row["digest_interval_seconds"]}


def _silence_view(row: sqlite3.Row, now: float) -> JSON:
    return {"id": str(row["id"]), "match": json.loads(str(row["match_json"])), "reason": str(row["reason"]), "expires_at": float(row["expires_at"]), "created_at": float(row["created_at"]), "active": float(row["expires_at"]) > now}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
