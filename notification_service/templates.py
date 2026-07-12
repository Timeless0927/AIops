"""Versioned, restricted Notification Template rendering."""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from aiops.contracts.notification import EVENT_TYPES, notification_request


JSON = dict[str, object]
PROVIDERS = ("feishu", "dingtalk", "smtp")
VARIABLES = (
    "event_type", "severity", "summary", "occurred_at", "console_path", "console_url",
    "subject_id", "environment", "team_id", "service_id", "cluster_id", "namespace",
    "resource_type", "resource_id",
)
_VARIABLE = re.compile(r"{{\s*([a-z_]+)\s*}}")
_FIELDS = {"title", "body", "color", "button_label", "subject"}


class NotificationTemplateError(ValueError):
    pass


class NotificationTemplates:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        console_base_url: str = "https://aiops.invalid",
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._console_base_url = console_base_url.rstrip("/")
        self._seed_builtins()
        self._freeze_unrendered_deliveries()

    def list(self) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.* FROM notification_templates t
                   WHERE NOT EXISTS (SELECT 1 FROM notification_templates newer WHERE newer.id = t.id AND newer.version > t.version)
                   ORDER BY t.is_builtin DESC, t.event_type, t.provider, t.name"""
            ).fetchall()
        return [_view(row) for row in rows]

    def get(self, template_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_templates WHERE id = ? ORDER BY version DESC LIMIT 1", (template_id,)
            ).fetchone()
        if row is None:
            raise NotificationTemplateError("template not found")
        return _view(row)

    def get_enabled(self, template_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_templates WHERE id = ? AND enabled = 1 ORDER BY version DESC LIMIT 1",
                (template_id,),
            ).fetchone()
        if row is None:
            raise NotificationTemplateError("template is not enabled")
        return _view(row)

    def copy(self, source_id: str, payload: JSON) -> JSON:
        if set(payload) != {"name"}:
            raise NotificationTemplateError("template copy supports only name")
        source = self.get(source_id)
        if not source["is_builtin"]:
            raise NotificationTemplateError("only a built-in template can be copied")
        name = _text(payload.get("name"), "name", 80)
        template_id = f"template:{uuid.uuid4().hex}"
        now = self._clock()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notification_templates VALUES (?, 1, ?, ?, ?, 0, 0, NULL, ?, ?, ?, ?, ?, ?)",
                (template_id, name, source["provider"], source["event_type"], source["title"], source["body"],
                 source["color"], source["button_label"], source["subject"], now),
            )
        return self.get(template_id)

    def update(self, template_id: str, payload: JSON) -> JSON:
        extras = set(payload) - (_FIELDS | {"enabled"})
        if extras:
            raise NotificationTemplateError(f"unsupported fields: {', '.join(sorted(extras))}")
        if not payload:
            raise NotificationTemplateError("template update is empty")
        current = self.get(template_id)
        if current["is_builtin"]:
            raise NotificationTemplateError("built-in template is immutable")
        if set(payload) == {"enabled"}:
            if not isinstance(payload["enabled"], bool):
                raise NotificationTemplateError("enabled must be boolean")
            if payload["enabled"] and current["validated_at"] is None:
                raise NotificationTemplateError("template must pass preview or test delivery before activation")
            with self._connect() as conn:
                if not payload["enabled"] and conn.execute(
                    "SELECT 1 FROM notification_routes WHERE enabled = 1 AND template_id = ? LIMIT 1", (template_id,)
                ).fetchone():
                    raise NotificationTemplateError("disable routes using this template first")
                if payload["enabled"]:
                    conn.execute("UPDATE notification_templates SET enabled = 0 WHERE id = ?", (template_id,))
                conn.execute(
                    "UPDATE notification_templates SET enabled = ? WHERE id = ? AND version = ?",
                    (int(payload["enabled"]), template_id, current["version"]),
                )
            return self.get(template_id)
        if "enabled" in payload:
            raise NotificationTemplateError("edit and activation must be separate")
        values = {field: payload.get(field, current[field]) for field in _FIELDS}
        _validate_presentation(str(current["provider"]), values)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notification_templates VALUES (?, ?, ?, ?, ?, 0, 0, NULL, ?, ?, ?, ?, ?, ?)",
                (template_id, int(current["version"]) + 1, current["name"], current["provider"], current["event_type"],
                 values["title"], values["body"], values["color"], values["button_label"], values["subject"], self._clock()),
            )
        return self.get(template_id)

    def preview(self, template_id: str, payload: JSON) -> JSON:
        template = self.get(template_id)
        rendered = self.render(template, payload)
        self.mark_validated(template)
        return rendered

    def render_id(self, template_id: str, payload: JSON) -> tuple[JSON, JSON]:
        template = self.get(template_id)
        return template, self.render(template, payload)

    def mark_validated(self, template: JSON) -> None:
        if not template["is_builtin"]:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE notification_templates SET validated_at = ? WHERE id = ? AND version = ?",
                    (self._clock(), template["id"], template["version"]),
                )

    def render_for(self, template_id: str | None, provider: str, payload: JSON) -> tuple[JSON, JSON]:
        request = notification_request(**payload)
        if template_id is None:
            template_id = f"template:builtin:{provider}:{request['event_type']}"
        template = self.get_enabled(template_id)
        if not template["enabled"] or template["provider"] != provider or template["event_type"] != request["event_type"]:
            raise NotificationTemplateError("template is not enabled or compatible")
        return template, self.render(template, request)

    def render(self, template: JSON, payload: JSON) -> JSON:
        request = notification_request(**payload)
        if template["event_type"] != request["event_type"]:
            raise NotificationTemplateError("template event is incompatible")
        values = _variables(request, self._console_base_url)
        rendered = {
            field: _render(str(template[field] or ""), values, markdown=field == "body")
            for field in ("title", "body", "color", "button_label", "subject")
        }
        rendered["body"] = f"{str(rendered['body']).rstrip()}\n\n[{_markdown_value(str(rendered['button_label']))}]({values['console_url']})"
        if template["provider"] == "smtp":
            rendered["html"] = _markdown_html(str(rendered["body"]))
            rendered["plain_text"] = _markdown_plain(str(rendered["body"]))
        return rendered

    def _seed_builtins(self) -> None:
        now = self._clock()
        with self._connect() as conn:
            for event in EVENT_TYPES:
                for provider in PROVIDERS:
                    conn.execute(
                        "INSERT OR IGNORE INTO notification_templates VALUES (?, 1, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (f"template:builtin:{provider}:{event}", f"{event} ({provider})", provider, event, now,
                         "{{severity}}: {{summary}}", "{{summary}}\n\nEvent: {{event_type}}\nSeverity: {{severity}}",
                         "#b42318", "Open in AIOps", "[AIOps] {{summary}}" if provider == "smtp" else None, now),
                    )

    def _freeze_unrendered_deliveries(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.id, x.provider, r.request_json
                   FROM notification_deliveries d
                   JOIN notification_destinations x ON x.id = d.destination
                   JOIN notification_requests r ON r.event_id = d.event_id
                   WHERE d.presentation_json IS NULL"""
            ).fetchall()
        frozen = []
        for row in rows:
            template, presentation = self.render_for(None, str(row["provider"]), json.loads(str(row["request_json"])))
            frozen.append((_json(presentation), template["id"], template["version"], row["id"]))
        if frozen:
            with self._connect() as conn:
                conn.executemany(
                    "UPDATE notification_deliveries SET presentation_json = ?, template_id = ?, template_version = ? WHERE id = ? AND presentation_json IS NULL",
                    frozen,
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _view(row: sqlite3.Row) -> JSON:
    return {
        "id": str(row["id"]), "version": int(row["version"]), "name": str(row["name"]),
        "provider": str(row["provider"]), "event_type": str(row["event_type"]),
        "is_builtin": bool(row["is_builtin"]), "enabled": bool(row["enabled"]),
        "validated_at": row["validated_at"], "title": str(row["title"]), "body": str(row["body"]),
        "color": str(row["color"]), "button_label": str(row["button_label"]), "subject": row["subject"],
    }


def _validate_presentation(provider: str, values: dict[str, object]) -> None:
    _text(values["title"], "title", 200)
    _text(values["body"], "body", 4000)
    color = _text(values["color"], "color", 7)
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise NotificationTemplateError("color must be a six-digit hex color")
    _text(values["button_label"], "button_label", 80)
    if provider == "smtp":
        _text(values["subject"], "subject", 200)
    elif values["subject"] not in {None, ""}:
        raise NotificationTemplateError("subject is supported only for SMTP")
    for value in values.values():
        if value:
            if re.search(r"<\s*/?\s*[A-Za-z][^>]*>", str(value)):
                raise NotificationTemplateError("arbitrary HTML is not supported")
            _validate_variables(str(value))


def _validate_variables(value: str) -> None:
    for variable in _VARIABLE.findall(value):
        if variable not in VARIABLES:
            raise NotificationTemplateError(f"unsupported template variable: {variable}")
    if "{{" in _VARIABLE.sub("", value) or "}}" in _VARIABLE.sub("", value) or "{%" in value or "%}" in value:
        raise NotificationTemplateError("unsupported template syntax")


def _variables(request: JSON, console_base_url: str) -> dict[str, str]:
    scope = request["scope"]
    subject = request["subject"]
    assert isinstance(scope, dict) and isinstance(subject, dict)
    values = {name: "" for name in VARIABLES}
    values.update({name: str(request.get(name, "")) for name in ("event_type", "severity", "summary", "occurred_at", "console_path")})
    values.update({name: str(scope.get(name, "")) for name in scope if name in values})
    values["subject_id"] = str(subject["id"])
    values["console_url"] = f"{console_base_url}{quote(str(request['console_path']), safe='/:?&=#%')}"
    return values


def _render(value: str, variables: dict[str, str], *, markdown: bool = False) -> str:
    _validate_variables(value)
    return _VARIABLE.sub(
        lambda match: _markdown_value(variables[match.group(1)]) if markdown else variables[match.group(1)], value
    )


def _markdown_value(value: str) -> str:
    escaped = html.escape(value)
    return re.sub(r"([\\`*_[\]{}()#+|])", r"\\\1", escaped)


def _markdown_html(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^]]+)]\((https?://[^ )]+)\)", r'<a href="\2">\1</a>', escaped)
    return "".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in escaped.split("\n\n") if part)


def _markdown_plain(value: str) -> str:
    value = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\1 (\2)", value)
    return value.replace("**", "").replace("__", "").replace("`", "")


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise NotificationTemplateError(f"{field} must be non-empty and at most {maximum} characters")
    return value.strip()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
