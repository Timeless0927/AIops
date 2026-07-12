"""Encrypted Notification Destinations and deterministic first-match routing."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aiops.contracts.notification import notification_request
from .apprise_adapter import send as apprise_send
from .apprise_adapter import send_result as apprise_send_result
from .notification_matching import matches, validate_match
from .noise_controls import NotificationNoiseControls
from .templates import NotificationTemplates, NotificationTemplateError


JSON = dict[str, object]
PROVIDERS = {"feishu", "dingtalk", "smtp"}
DEFAULT_ROUTE_ID = "route:default-suppress"


NotificationConfigurationError = NotificationTemplateError


class NotificationConfiguration:
    def __init__(
        self,
        db_path: Path | str,
        key_path: Path | str,
        noise: NotificationNoiseControls,
        *,
        clock: Callable[[], float] = time.time,
        send: Callable[[str, str, str], bool] | None = None,
        console_base_url: str = "https://aiops.invalid",
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._key = _read_key(Path(key_path))
        self._send = send
        from .requests import migrate_notification_database

        migrate_notification_database(self.db_path)
        self.templates = NotificationTemplates(self.db_path, clock=clock, console_base_url=console_base_url)
        self._noise = noise

    def list_templates(self) -> list[JSON]:
        return self.templates.list()

    def copy_template(self, source_id: str, payload: JSON) -> JSON:
        return self.templates.copy(source_id, payload)

    def update_template(self, template_id: str, payload: JSON) -> JSON:
        return self.templates.update(template_id, payload)

    def preview_template(self, template_id: str, payload: JSON) -> JSON:
        return self.templates.preview(template_id, payload)

    def test_template(self, template_id: str, destination_id: str, payload: JSON) -> JSON:
        template, rendered = self.templates.render_id(template_id, payload)
        provider, _config = self.delivery_config(destination_id)
        if provider != template["provider"]:
            raise NotificationConfigurationError("template and destination providers are incompatible")
        title = str(rendered.get("subject") or rendered["title"])
        body = str(rendered.get("html") or rendered["body"])
        if not self.send(destination_id, title, body):
            raise NotificationConfigurationError("template test delivery failed")
        self.templates.mark_validated(template)
        return {"template": self.templates.get(template_id), "preview": rendered}

    def list_destinations(self) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_destinations ORDER BY name, id").fetchall()
        return [self._destination_view(row) for row in rows]

    def create_destination(self, payload: JSON) -> JSON:
        _only_fields(payload, {"name", "provider", "config"})
        name = _text(payload, "name", 80)
        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in PROVIDERS:
            raise NotificationConfigurationError("provider must be feishu, dingtalk, or smtp")
        config = _validate_provider_config(provider, payload.get("config"))
        destination_id = f"destination:{uuid.uuid4().hex}"
        now = self._clock()
        ciphertext = self._encrypt(config)
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO notification_destinations VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
                    (destination_id, name, provider, ciphertext, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise NotificationConfigurationError("destination name already exists") from exc
        return self.get_destination(destination_id)

    def get_destination(self, destination_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone()
        if row is None:
            raise NotificationConfigurationError("destination not found")
        return self._destination_view(row)

    def update_destination(self, destination_id: str, payload: JSON) -> JSON:
        _only_fields(payload, {"name", "config", "enabled"})
        if not payload:
            raise NotificationConfigurationError("destination update is empty")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone()
            if row is None:
                raise NotificationConfigurationError("destination not found")
            name = _text(payload, "name", 80) if "name" in payload else str(row["name"])
            config = self._decrypt(str(row["config_ciphertext"]))
            tested_at = row["tested_at"]
            if "config" in payload:
                config = _validate_provider_config(str(row["provider"]), payload["config"])
                tested_at = None
            if "enabled" in payload and not isinstance(payload["enabled"], bool):
                raise NotificationConfigurationError("enabled must be boolean")
            enabled = bool(payload.get("enabled", row["enabled"]))
            if enabled and tested_at is None:
                raise NotificationConfigurationError("destination must pass test delivery before activation")
            if row["enabled"] and not enabled:
                routes = conn.execute("SELECT destination_ids_json FROM notification_routes WHERE enabled = 1").fetchall()
                if any(destination_id in json.loads(str(route[0])) for route in routes):
                    raise NotificationConfigurationError("disable routes using this destination first")
            try:
                conn.execute(
                    "UPDATE notification_destinations SET name = ?, config_ciphertext = ?, enabled = ?, tested_at = ?, updated_at = ? WHERE id = ?",
                    (name, self._encrypt(config), int(enabled), tested_at, self._clock(), destination_id),
                )
            except sqlite3.IntegrityError as exc:
                raise NotificationConfigurationError("destination name already exists") from exc
        return self.get_destination(destination_id)

    def test_destination(self, destination_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone()
            if row is None:
                raise NotificationConfigurationError("destination not found")
            config = self._decrypt(str(row["config_ciphertext"]))
        try:
            delivered = self._deliver(_apprise_url(str(row["provider"]), config), "AIOps test", "Notification destination test")
        except Exception as exc:
            raise NotificationConfigurationError(f"test delivery failed: {type(exc).__name__}") from exc
        if not delivered:
            raise NotificationConfigurationError("test delivery failed")
        tested_at = self._clock()
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_destinations SET tested_at = ?, updated_at = ? WHERE id = ?",
                (tested_at, tested_at, destination_id),
            )
        return self.get_destination(destination_id)

    def list_routes(self) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_routes ORDER BY priority, id").fetchall()
        return [_route_view(row) for row in rows]

    def create_route(self, payload: JSON) -> JSON:
        _only_fields(payload, {"name", "priority", "enabled", "match", "destination_ids", "suppress_reason", "template_id"})
        route_id = f"route:{uuid.uuid4().hex}"
        values = self._validated_route(payload)
        now = self._clock()
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO notification_routes
                       (id, name, priority, enabled, match_json, destination_ids_json, suppress_reason, is_default, template_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (route_id, *values, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise NotificationConfigurationError("route name or priority already exists") from exc
        return self.get_route(route_id)

    def get_route(self, route_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_routes WHERE id = ?", (route_id,)).fetchone()
        if row is None:
            raise NotificationConfigurationError("route not found")
        return _route_view(row)

    def update_route(self, route_id: str, payload: JSON) -> JSON:
        _only_fields(payload, {"name", "priority", "enabled", "match", "destination_ids", "suppress_reason", "template_id"})
        if not payload:
            raise NotificationConfigurationError("route update is empty")
        if route_id == DEFAULT_ROUTE_ID:
            raise NotificationConfigurationError("built-in default route is immutable")
        current = self.get_route(route_id)
        merged = {**current, **payload}
        values = self._validated_route(merged)
        with self._connect() as conn:
            try:
                conn.execute(
                    "UPDATE notification_routes SET name = ?, priority = ?, enabled = ?, match_json = ?, destination_ids_json = ?, suppress_reason = ?, template_id = ?, updated_at = ? WHERE id = ?",
                    (*values, self._clock(), route_id),
                )
            except sqlite3.IntegrityError as exc:
                raise NotificationConfigurationError("route name or priority already exists") from exc
        return self.get_route(route_id)

    def route(self, payload: JSON) -> JSON:
        request = notification_request(**payload)
        for route in self.list_routes():
            if route["enabled"] and matches(route["match"], request):
                destination_ids = list(dict.fromkeys(route["destination_ids"]))
                if destination_ids:
                    active = {str(item["id"]): item for item in self.list_destinations() if item["enabled"]}
                    if any(item not in active for item in destination_ids):
                        raise NotificationConfigurationError("route references an inactive destination")
                deliveries = []
                for destination_id in destination_ids:
                    template, presentation = self.templates.render_for(
                        str(route["template_id"]) if route["template_id"] else None,
                        str(active[destination_id]["provider"]),
                        request,
                    )
                    deliveries.append({"destination_id": destination_id, "template_id": template["id"], "template_version": template["version"], "presentation": presentation})
                return {
                    "route_id": route["id"],
                    "route_name": route["name"],
                    "destination_ids": destination_ids,
                    "suppressed_reason": route["suppress_reason"],
                    "deliveries": deliveries,
                }
        raise NotificationConfigurationError("final default route is missing")

    def simulate(self, payload: JSON) -> JSON:
        result = self.route(payload)
        destinations = {str(item["id"]): item for item in self.list_destinations()}
        return {**{key: value for key, value in result.items() if key != "deliveries"}, "destinations": [destinations[item] for item in result["destination_ids"]]}

    def delivery_config(self, destination_id: str) -> tuple[str, JSON]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT provider, config_ciphertext, enabled FROM notification_destinations WHERE id = ?",
                (destination_id,),
            ).fetchone()
        if row is None or not row["enabled"]:
            raise NotificationConfigurationError("destination is unavailable")
        return str(row["provider"]), self._decrypt(str(row["config_ciphertext"]))

    def send(self, destination_id: str, title: str, body: str, *, body_format: str = "text") -> bool:
        provider, config = self.delivery_config(destination_id)
        return self._deliver(_apprise_url(provider, config), title, body, body_format=body_format)

    def delivery_result(self, destination_id: str, title: str, body: str, *, body_format: str = "text") -> JSON:
        provider, config = self.delivery_config(destination_id)
        url = _apprise_url(provider, config)
        if self._send is not None:
            return {"ok": bool(self._send(url, title, body))}
        try:
            return apprise_send_result(url, title, body, body_format=body_format)
        except ValueError as exc:
            raise NotificationConfigurationError(str(exc)) from exc

    def _deliver(self, url: str, title: str, body: str, *, body_format: str = "text") -> bool:
        if self._send is not None:
            return self._send(url, title, body)
        return apprise_send(url, title, body, body_format=body_format)

    def _validated_route(self, payload: JSON) -> tuple[object, ...]:
        name = _text(payload, "name", 80)
        priority = payload.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority < 2_147_483_647:
            raise NotificationConfigurationError("priority must be an integer between 0 and 2147483646")
        match = validate_match(payload.get("match", {}), owner="route")
        destination_ids = payload.get("destination_ids", [])
        if not isinstance(destination_ids, list) or not all(isinstance(item, str) and item for item in destination_ids):
            raise NotificationConfigurationError("destination_ids must be a list of IDs")
        destination_ids = list(dict.fromkeys(destination_ids))
        reason = str(payload.get("suppress_reason") or "").strip() or None
        if bool(destination_ids) == bool(reason):
            raise NotificationConfigurationError("route must fan out or suppress")
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            raise NotificationConfigurationError("enabled must be boolean")
        enabled = bool(payload.get("enabled", False))
        if enabled and destination_ids:
            active = {str(item["id"]) for item in self.list_destinations() if item["enabled"]}
            if any(item not in active for item in destination_ids):
                raise NotificationConfigurationError("route references an inactive destination")
        template_id = payload.get("template_id") or None
        if template_id:
            if not isinstance(template_id, str):
                raise NotificationConfigurationError("template_id must be a string")
            template = self.templates.get_enabled(template_id)
            destinations = {str(item["id"]): item for item in self.list_destinations()}
            events = match.get("event")
            if not template["enabled"] or events != [template["event_type"]] or any(
                item not in destinations or destinations[item]["provider"] != template["provider"] for item in destination_ids
            ):
                raise NotificationConfigurationError("route template must be enabled and compatible with its event and destinations")
        return name, priority, int(enabled), _json(match), _json(destination_ids), reason, template_id

    def _destination_view(self, row: sqlite3.Row) -> JSON:
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "provider": str(row["provider"]),
            "enabled": bool(row["enabled"]),
            "tested_at": row["tested_at"],
            "config": _masked_config(str(row["provider"]), self._decrypt(str(row["config_ciphertext"]))),
            "noise_control": self._noise.get_destination(str(row["id"])),
        }

    def _encrypt(self, value: JSON) -> str:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, _json(value).encode(), b"aiops-notification-destination-v1")
        return base64.urlsafe_b64encode(nonce + encrypted).decode()

    def _decrypt(self, encoded: str) -> JSON:
        raw = base64.urlsafe_b64decode(encoded)
        value = AESGCM(self._key).decrypt(raw[:12], raw[12:], b"aiops-notification-destination-v1")
        return json.loads(value)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _read_key(path: Path) -> bytes:
    try:
        value = path.read_bytes().strip()
    except OSError as exc:
        raise NotificationConfigurationError(f"destination encryption key is unavailable: {path}") from exc
    try:
        decoded = base64.urlsafe_b64decode(value)
    except ValueError:
        decoded = b""
    key = decoded if len(decoded) == 32 else value
    if len(key) != 32:
        raise NotificationConfigurationError("destination encryption key must contain 32 raw or base64-encoded bytes")
    return key


def _validate_provider_config(provider: str, value: object) -> JSON:
    if not isinstance(value, dict):
        raise NotificationConfigurationError("config must be an object")
    if provider in {"feishu", "dingtalk"}:
        allowed = {"webhook_url"} if provider == "feishu" else {"webhook_url", "signing_secret"}
        if set(value) - allowed:
            raise NotificationConfigurationError("unsupported provider configuration field")
        webhook = _text(value, "webhook_url", 2048)
        parsed = urlparse(webhook)
        expected_host = "open.feishu.cn" if provider == "feishu" else "oapi.dingtalk.com"
        expected_prefix = "/open-apis/bot/v2/hook/" if provider == "feishu" else "/robot/send"
        if parsed.scheme != "https" or parsed.hostname != expected_host or parsed.username or parsed.port or parsed.fragment:
            raise NotificationConfigurationError(f"invalid {provider} group bot webhook URL")
        config: JSON = {"webhook_url": webhook}
        if provider == "dingtalk":
            config["signing_secret"] = _text(value, "signing_secret", 512)
            if parsed.path != expected_prefix or set(parse_qs(parsed.query)) != {"access_token"}:
                raise NotificationConfigurationError("invalid dingtalk group bot webhook URL")
            token = parse_qs(parsed.query).get("access_token", [""])[0]
            if not token.isalnum() or not str(config["signing_secret"]).isalnum():
                raise NotificationConfigurationError("invalid dingtalk token or signing secret")
        else:
            token = parsed.path[len(expected_prefix):].strip("/") if parsed.path.startswith(expected_prefix) else ""
            if parsed.query or "/" in token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                raise NotificationConfigurationError("invalid feishu token")
        return config
    allowed = {"host", "port", "username", "password", "from_address", "to_addresses", "tls_mode"}
    if set(value) - allowed:
        raise NotificationConfigurationError("unsupported provider configuration field")
    port = value.get("port")
    recipients = value.get("to_addresses")
    tls_mode = value.get("tls_mode")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise NotificationConfigurationError("SMTP port is invalid")
    if tls_mode not in {"starttls", "ssl"}:
        raise NotificationConfigurationError("SMTP TLS mode must be starttls or ssl")
    if not isinstance(recipients, list) or not recipients or not all(isinstance(item, str) and "@" in item for item in recipients):
        raise NotificationConfigurationError("SMTP recipients are invalid")
    return {
        "host": _text(value, "host", 253),
        "port": port,
        "username": _text(value, "username", 320),
        "password": _text(value, "password", 1024),
        "from_address": _email(value, "from_address"),
        "to_addresses": recipients,
        "tls_mode": tls_mode,
    }


def _masked_config(provider: str, config: JSON) -> JSON:
    if provider in {"feishu", "dingtalk"}:
        parsed = urlparse(str(config["webhook_url"]))
        return {
            "webhook_url": f"{parsed.scheme}://{parsed.netloc}/***",
            "signing_secret_configured": bool(config.get("signing_secret")),
        }
    return {
        "host": config["host"],
        "port": config["port"],
        "username": config["username"],
        "from_address": config["from_address"],
        "to_addresses": config["to_addresses"],
        "tls_mode": config["tls_mode"],
        "password_configured": True,
    }


def _apprise_url(provider: str, config: JSON) -> str:
    if provider == "feishu":
        token = urlparse(str(config["webhook_url"])).path.rstrip("/").rsplit("/", 1)[-1]
        return f"feishu://{quote(unquote(token), safe='')}"
    if provider == "dingtalk":
        token = parse_qs(urlparse(str(config["webhook_url"])).query)["access_token"][0]
        return f"dingtalk://{quote(str(config['signing_secret']), safe='')}@{quote(unquote(token), safe='')}"
    scheme = "mailtos" if config["tls_mode"] == "ssl" else "mailto"
    recipients = "/".join(quote(str(item), safe="@") for item in config["to_addresses"])
    query = f"smtp={quote(str(config['host']), safe='')}&from={quote(str(config['from_address']), safe='@')}&mode={config['tls_mode']}"
    return f"{scheme}://{quote(str(config['username']), safe='')}:{quote(str(config['password']), safe='')}@{config['host']}:{config['port']}/{recipients}?{query}"


def _route_view(row: sqlite3.Row) -> JSON:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "match": json.loads(str(row["match_json"])),
        "destination_ids": json.loads(str(row["destination_ids_json"])),
        "suppress_reason": row["suppress_reason"],
        "is_default": bool(row["is_default"]),
        "template_id": row["template_id"],
    }


def _text(value: object, field: str, maximum: int) -> str:
    item = value.get(field) if isinstance(value, dict) else None
    if not isinstance(item, str) or not item.strip() or len(item.strip()) > maximum:
        raise NotificationConfigurationError(f"{field} is required")
    return item.strip()


def _only_fields(value: JSON, allowed: set[str]) -> None:
    extras = set(value) - allowed
    if extras:
        raise NotificationConfigurationError(f"unsupported fields: {', '.join(sorted(extras))}")


def _email(value: object, field: str) -> str:
    item = _text(value, field, 320)
    if "@" not in item:
        raise NotificationConfigurationError(f"{field} is invalid")
    return item


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
