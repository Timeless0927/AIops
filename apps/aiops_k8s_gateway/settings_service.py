"""Versioned Console settings and policy explanation store."""

from __future__ import annotations

import copy
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar


JSON = dict[str, Any]
T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50
CONFIRM_TEXT = "CONFIRM SETTINGS CHANGE"
ACTION_TYPES = (
    "k8s_read",
    "restart_deployment",
    "scale_deployment",
    "rollback_deployment",
    "patch_config",
    "toggle_feature",
    "notify_only",
)
RISK_LEVELS = ("read_only", "low", "medium", "high")
SECRET_MARKERS = ("secret", "token", "password", "bind_password", "url", "db", "path")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings_versions (
    version_id TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL UNIQUE,
    settings_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    change_summary TEXT NOT NULL,
    critical_confirmed INTEGER NOT NULL DEFAULT 0,
    reload_required INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS policy_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    when_ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    cluster TEXT NOT NULL,
    namespace TEXT,
    service TEXT,
    team TEXT,
    environment TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    settings_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settings_versions_number
ON settings_versions(version_number DESC);

CREATE INDEX IF NOT EXISTS idx_policy_hits_when
ON policy_hits(when_ts DESC);
"""


class SettingsServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def default_settings() -> JSON:
    return {
        "clusters": [],
        "approval_policy": {
            "rules": default_policy_rules(),
            "allow_self_approval_low_risk": False,
            "dev_low_risk_auto_execute": False,
            "test_low_risk_auto_execute": False,
        },
        "action_allowlist": default_action_allowlist(),
        "notifications": {"console_center": True, "external_link_only": True},
        "security": {"session_cookie": "HttpOnly; SameSite=Lax", "csrf_required": True},
        "feature_flags": {
            "openobserve_evidence": False,
            "agent_runs_sse": False,
            "mobile_approval": False,
        },
    }


def default_policy_rules() -> list[JSON]:
    rules: list[JSON] = [
        _policy_rule("prod", "*", "read_only", False, True, False, ["operator", "approver", "admin"]),
        _policy_rule("prod", "*", "low", True, True, False, ["approver", "admin"]),
        _policy_rule("prod", "*", "medium", True, True, False, ["approver", "admin"]),
        _policy_rule("prod", "*", "high", True, True, False, ["approver", "admin"]),
        _policy_rule("staging", "*", "read_only", False, True, False, ["operator", "approver", "admin"]),
        _policy_rule("staging", "*", "low", False, False, True, ["approver", "admin"]),
        _policy_rule("staging", "*", "medium", True, True, False, ["approver", "admin"]),
        _policy_rule("staging", "*", "high", True, True, False, ["approver", "admin"]),
        _policy_rule("dev", "*", "read_only", False, True, False, ["operator", "approver", "admin"]),
        _policy_rule("dev", "*", "low", False, False, True, ["approver", "admin"]),
        _policy_rule("dev", "*", "medium", False, False, True, ["approver", "admin"]),
        _policy_rule("dev", "*", "high", True, True, False, ["approver", "admin"]),
        _policy_rule("test", "*", "read_only", False, True, False, ["operator", "approver", "admin"]),
        _policy_rule("test", "*", "low", False, False, True, ["approver", "admin"]),
        _policy_rule("test", "*", "medium", False, False, True, ["approver", "admin"]),
        _policy_rule("test", "*", "high", True, True, False, ["approver", "admin"]),
    ]
    return rules


def _policy_rule(
    environment: str,
    action_type: str,
    risk_level: str,
    approval_required: bool,
    auto_execution: bool,
    self_approval: bool,
    eligible_approver_roles: list[str],
    *,
    cluster: str | None = None,
    namespace: str | None = None,
) -> JSON:
    return {
        "environment": environment,
        "cluster": cluster,
        "namespace": namespace,
        "action_type": action_type,
        "risk_level": risk_level,
        "approval_required": approval_required,
        "auto_execution": auto_execution,
        "self_approval": self_approval,
        "eligible_approver_roles": eligible_approver_roles,
    }


def default_action_allowlist() -> list[JSON]:
    return [
        _allowlist("k8s_read", "k8s", "kubectl get {resource}", ["prod", "staging", "dev", "test"], "read_only", True, False, False, True),
        _allowlist("restart_deployment", "k8s", "kubectl rollout restart deployment/{service}", ["prod", "staging", "dev", "test"], "low", True, True, True, True),
        _allowlist("scale_deployment", "k8s", "kubectl scale deployment/{service} --replicas={replicas}", ["staging", "dev", "test"], "medium", True, True, True, True),
        _allowlist("rollback_deployment", "k8s", "kubectl rollout undo deployment/{service}", ["prod", "staging"], "high", True, True, True, True),
        _allowlist("patch_config", "deployment", "apply config patch {change_id}", ["staging", "dev", "test"], "medium", True, True, True, False),
        _allowlist("toggle_feature", "feature_flag", "set feature {flag}={state}", ["staging", "dev", "test"], "low", True, True, True, False),
        _allowlist("notify_only", "notification", "send notification {channel}", ["prod", "staging", "dev", "test"], "read_only", False, False, False, True),
    ]


def _allowlist(
    action_type: str,
    backend: str,
    template: str,
    allowed_scopes: list[str],
    default_risk: str,
    preflight: bool,
    post_check: bool,
    rollback_required: bool,
    enabled: bool,
) -> JSON:
    return {
        "action_type": action_type,
        "backend": backend,
        "template": template,
        "allowed_scopes": allowed_scopes,
        "default_risk": default_risk,
        "preflight": preflight,
        "post_check": post_check,
        "rollback_required": rollback_required,
        "enabled": enabled,
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "settings.db"
    return _project_root() / "data" / "settings.db"


class SettingsDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
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

    def current(self) -> JSON:
        self._ensure_seed()
        row = self._fetchone("SELECT * FROM settings_versions ORDER BY version_number DESC LIMIT 1")
        if row is None:
            raise SettingsServiceError("settings_unavailable", "settings are unavailable", status=500)
        row["settings"] = normalize_settings(strip_secret_keys(row["settings"]))
        return row

    def preview(self, payload: JSON) -> JSON:
        current = self.current()
        new_settings = normalize_settings(payload.get("settings") if isinstance(payload.get("settings"), dict) else payload)
        diff = diff_settings(current["settings"], new_settings)
        return {
            "settings": new_settings,
            "diff": diff,
            "critical": is_critical_diff(diff),
            "confirmation_text": CONFIRM_TEXT,
            "reload_required": reload_required(diff),
        }

    def save(self, payload: JSON, *, actor_id: str) -> JSON:
        preview = self.preview(payload)
        confirmation = str(payload.get("confirmation") or "").strip()
        critical = bool(preview["critical"])
        if critical and confirmation != CONFIRM_TEXT:
            raise SettingsServiceError("confirmation_required", "critical settings change requires exact confirmation", status=409)
        return self._insert_version(
            preview["settings"],
            preview["diff"],
            actor_id=actor_id,
            summary=str(payload.get("change_summary") or "settings saved"),
            critical_confirmed=critical,
            reload_required=bool(preview["reload_required"]),
        )

    def rollback(self, *, actor_id: str) -> JSON:
        self._ensure_seed()
        rows = self._fetchall("SELECT * FROM settings_versions ORDER BY version_number DESC LIMIT 2")
        if len(rows) < 2:
            raise SettingsServiceError("rollback_unavailable", "no previous settings version exists", status=409)
        current, previous = self.current(), rows[1]
        previous_settings = normalize_settings(strip_secret_keys(previous["settings"]))
        diff = diff_settings(current["settings"], previous_settings)
        return self._insert_version(
            previous_settings,
            diff,
            actor_id=actor_id,
            summary=f"rollback to version {previous['version_number']}",
            critical_confirmed=True,
            reload_required=reload_required(diff),
        )

    def policy_state(self) -> JSON:
        current = self.current()
        settings = current["settings"]
        return {
            "version": current["version_number"],
            "cluster_environments": settings["clusters"],
            "default_environment": "prod",
            "action_allowlist": settings["action_allowlist"],
            "policy": _policy_description(settings),
            "recent_policy_hits": self.recent_policy_hits(),
        }

    def test_policy(self, payload: JSON, *, actor_id: str) -> JSON:
        current = self.current()
        result = classify_action(current["settings"], payload)
        hit = {
            "when_ts": time.time(),
            "actor": actor_id,
            "action_type": result["action_type"],
            "cluster": result["cluster"],
            "namespace": result.get("namespace"),
            "service": result.get("service"),
            "team": result.get("team"),
            "environment": result["environment"],
            "decision": result["decision"],
            "reason": result["reason"],
            "settings_version": current["version_number"],
        }

        def _write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO policy_hits (
                    when_ts, actor, action_type, cluster, namespace, service, team,
                    environment, decision, reason, settings_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hit["when_ts"],
                    hit["actor"],
                    hit["action_type"],
                    hit["cluster"],
                    hit["namespace"],
                    hit["service"],
                    hit["team"],
                    hit["environment"],
                    hit["decision"],
                    hit["reason"],
                    hit["settings_version"],
                ),
            )
            return int(cursor.lastrowid)

        hit["id"] = self._execute_write(_write)
        return {"result": result, "policy_hit": hit}

    def recent_policy_hits(self, *, limit: int = 20) -> list[JSON]:
        return self._fetchall(
            """
            SELECT id, when_ts, actor, action_type, cluster, namespace, service, team,
                   environment, decision, reason, settings_version
            FROM policy_hits
            ORDER BY when_ts DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        )

    def _insert_version(
        self,
        settings: JSON,
        diff: list[JSON],
        *,
        actor_id: str,
        summary: str,
        critical_confirmed: bool,
        reload_required: bool,
    ) -> JSON:
        now = time.time()
        version_id = f"cfg-{uuid.uuid4().hex}"

        def _write(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT COALESCE(MAX(version_number), 0) AS number FROM settings_versions").fetchone()
            number = int(row["number"] or 0) + 1
            conn.execute(
                """
                INSERT INTO settings_versions (
                    version_id, version_number, settings_json, diff_json, created_by,
                    created_at, change_summary, critical_confirmed, reload_required
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    number,
                    stable_json(settings),
                    stable_json(diff),
                    actor_id,
                    now,
                    summary.strip() or "settings saved",
                    1 if critical_confirmed else 0,
                    1 if reload_required else 0,
                ),
            )
            return number

        number = self._execute_write(_write)
        row = self._fetchone("SELECT * FROM settings_versions WHERE version_number = ?", (number,))
        if row is None:
            raise SettingsServiceError("settings_unavailable", "settings version was not persisted", status=500)
        return row

    def _ensure_seed(self) -> None:
        if self._fetchone("SELECT version_number FROM settings_versions LIMIT 1") is not None:
            return
        self._insert_version(
            default_settings(),
            [],
            actor_id="system",
            summary="initial defaults",
            critical_confirmed=True,
            reload_required=False,
        )

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

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return _decode_row(dict(row)) if row is not None else None

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_row(dict(row)) for row in rows]


def normalize_settings(payload: JSON) -> JSON:
    if contains_secret_key(payload):
        raise SettingsServiceError("secret_field_forbidden", "secret-like settings are not editable or visible", status=400)
    base = default_settings()
    value = deep_merge(base, payload)
    clusters = value.get("clusters") if isinstance(value.get("clusters"), list) else []
    normalized_clusters: list[JSON] = []
    for item in clusters:
        if not isinstance(item, dict):
            continue
        cluster = str(item.get("cluster") or item.get("cluster_id") or "").strip()
        environment = str(item.get("environment") or "prod").strip().lower()
        if cluster:
            normalized_clusters.append({"cluster": cluster, "environment": normalize_environment(environment)})
    value["clusters"] = normalized_clusters
    value["approval_policy"] = normalize_approval_policy(value.get("approval_policy"))
    value["action_allowlist"] = normalize_action_allowlist(value.get("action_allowlist"))
    return value


def normalize_approval_policy(value: Any) -> JSON:
    policy = value if isinstance(value, dict) else {}
    normalized = {
        "rules": normalize_policy_rules(policy.get("rules")),
        "allow_self_approval_low_risk": bool(policy.get("allow_self_approval_low_risk", False)),
        "dev_low_risk_auto_execute": bool(policy.get("dev_low_risk_auto_execute", False)),
        "test_low_risk_auto_execute": bool(policy.get("test_low_risk_auto_execute", False)),
    }
    return normalized


def normalize_policy_rules(value: Any) -> list[JSON]:
    rules = value if isinstance(value, list) else default_policy_rules()
    normalized: list[JSON] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        environment = normalize_environment(str(item.get("environment") or "prod").strip().lower())
        risk_level = normalize_risk(str(item.get("risk_level") or "low").strip().lower())
        action_type = normalize_action_type(str(item.get("action_type") or "*").strip()) or "*"
        normalized.append(
            {
                "environment": environment,
                "cluster": optional_text(item.get("cluster")),
                "namespace": optional_text(item.get("namespace")),
                "action_type": action_type,
                "risk_level": risk_level,
                "approval_required": bool(item.get("approval_required", False)),
                "auto_execution": bool(item.get("auto_execution", False)),
                "self_approval": bool(item.get("self_approval", False)),
                "eligible_approver_roles": normalize_roles(item.get("eligible_approver_roles")),
            }
        )
    return normalized or default_policy_rules()


def normalize_action_allowlist(value: Any) -> list[JSON]:
    defaults = {item["action_type"]: item for item in default_action_allowlist()}
    entries = value if isinstance(value, list) else []
    normalized: list[JSON] = []
    for item in entries:
        if isinstance(item, str):
            base = copy.deepcopy(defaults.get(item))
            if base:
                normalized.append(base)
            continue
        if not isinstance(item, dict):
            continue
        action_type = normalize_action_type(str(item.get("action_type") or "").strip())
        if not action_type or action_type == "*":
            continue
        base = copy.deepcopy(defaults.get(action_type, {}))
        merged = deep_merge(base, item) if base else copy.deepcopy(item)
        normalized.append(
            {
                "action_type": action_type,
                "backend": str(merged.get("backend") or "k8s").strip(),
                "template": str(merged.get("template") or "").strip(),
                "allowed_scopes": normalize_scopes(merged.get("allowed_scopes")),
                "default_risk": normalize_risk(str(merged.get("default_risk") or "low").strip().lower()),
                "preflight": bool(merged.get("preflight", True)),
                "post_check": bool(merged.get("post_check", True)),
                "rollback_required": bool(merged.get("rollback_required", False)),
                "enabled": bool(merged.get("enabled", True)),
            }
        )
    return normalized or default_action_allowlist()


def classify_action(settings: JSON, payload: JSON) -> JSON:
    action_type = str(payload.get("action_type") or payload.get("action") or "").strip()
    cluster = str(payload.get("cluster") or payload.get("cluster_id") or "").strip() or "unconfigured"
    environment = environment_for_cluster(settings, cluster)
    allow = allowlist_record(settings, action_type)
    risk = normalize_risk(str(payload.get("risk_level") or (allow or {}).get("default_risk") or "low").strip().lower())
    rule = matching_policy_rule(settings, environment=environment, cluster=cluster, namespace=payload.get("namespace"), action_type=action_type, risk_level=risk)
    if allow is None or not allow.get("enabled", False):
        decision, reason = "denied", "action_not_allowlisted"
    elif not scope_allowed(allow.get("allowed_scopes", []), environment=environment, payload=payload):
        decision, reason = "denied", "scope_not_allowlisted"
    else:
        decision, reason = "approval_required", f"{environment}_{risk}_requires_explicit_approval"
    return {
        "action_type": action_type,
        "cluster": cluster,
        "namespace": str(payload.get("namespace") or "").strip() or None,
        "service": str(payload.get("service") or "").strip() or None,
        "team": str(payload.get("team") or "").strip() or None,
        "risk_level": risk,
        "environment": environment,
        "decision": decision,
        "reason": reason,
        "approval_required": bool(rule.get("approval_required", False)),
        "auto_execution": bool(rule.get("auto_execution", False)),
        "self_approval": bool(rule.get("self_approval", False)),
        "eligible_approver_roles": rule.get("eligible_approver_roles", []),
    }


def allowlist_record(settings: JSON, action_type: str) -> JSON | None:
    for item in settings.get("action_allowlist", []):
        if isinstance(item, dict) and item.get("action_type") == action_type:
            return item
        if isinstance(item, str) and item == action_type:
            return {"action_type": item, "allowed_scopes": ["prod", "staging", "dev", "test"], "default_risk": "low", "enabled": True}
    return None


def matching_policy_rule(
    settings: JSON,
    *,
    environment: str,
    cluster: str,
    namespace: Any,
    action_type: str,
    risk_level: str,
) -> JSON:
    namespace_text = optional_text(namespace)
    rules = settings.get("approval_policy", {}).get("rules", default_policy_rules())
    candidates = []
    for rule in rules:
        if rule.get("environment") != environment:
            continue
        if rule.get("risk_level") != risk_level:
            continue
        if rule.get("action_type") not in {action_type, "*"}:
            continue
        if rule.get("cluster") and rule.get("cluster") != cluster:
            continue
        if rule.get("namespace") and rule.get("namespace") != namespace_text:
            continue
        score = int(bool(rule.get("cluster"))) + int(bool(rule.get("namespace"))) + int(rule.get("action_type") == action_type)
        candidates.append((score, rule))
    if candidates:
        return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]
    if environment == "prod" and risk_level in {"low", "medium", "high"}:
        return _policy_rule("prod", "*", risk_level, True, True, False, ["approver", "admin"])
    return _policy_rule(environment, "*", risk_level, False, False, True, ["approver", "admin"])


def environment_for_cluster(settings: JSON, cluster: str) -> str:
    for item in settings.get("clusters", []):
        if item.get("cluster") == cluster:
            return normalize_environment(str(item.get("environment") or "prod"))
    return "prod"


def diff_settings(old: JSON, new: JSON) -> list[JSON]:
    result: list[JSON] = []
    keys = sorted(set(old) | set(new))
    for key in keys:
        if old.get(key) != new.get(key):
            result.append({"path": key, "before": old.get(key), "after": new.get(key)})
    return result


def is_critical_diff(diff: list[JSON]) -> bool:
    return any(item["path"] in {"approval_policy", "action_allowlist", "clusters", "security"} for item in diff)


def reload_required(diff: list[JSON]) -> bool:
    return any(item["path"] in {"security", "feature_flags"} for item in diff)


def deep_merge(base: JSON, update: JSON) -> JSON:
    merged = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_MARKERS):
                return True
            if contains_secret_key(item):
                return True
    if isinstance(value, list):
        return any(contains_secret_key(item) for item in value)
    return False


def normalize_environment(value: str) -> str:
    return value if value in {"prod", "staging", "dev", "test"} else "prod"


def _policy_description(settings: JSON) -> JSON:
    return {
        "rules": settings["approval_policy"].get("rules", default_policy_rules()),
        "prod": "low, medium, and high mutations require approval; unconfigured clusters are prod",
        "staging": "low-risk mutations may receive policy grants; medium/high require approval",
        "dev": "low-risk automatic execution is configurable",
        "test": "low-risk automatic execution is configurable",
        "allow_self_approval_low_risk": settings["approval_policy"].get("allow_self_approval_low_risk", False),
    }


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_action_type(value: str) -> str:
    return value if value in ACTION_TYPES or value == "*" else ""


def normalize_risk(value: str) -> str:
    return value if value in RISK_LEVELS else "low"


def normalize_roles(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value]
    else:
        raw = ["approver", "admin"]
    return [item for item in raw if item in {"viewer", "operator", "approver", "auditor", "admin"}] or ["approver", "admin"]


def normalize_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value]
    else:
        raw = ["prod", "staging", "dev", "test"]
    allowed: list[str] = []
    for item in raw:
        if not item:
            continue
        if item in {"prod", "staging", "dev", "test", "*"} or item.startswith(("cluster:", "namespace:", "service:", "team:")):
            allowed.append(item)
    return allowed or ["prod"]


def scope_allowed(scopes: Any, *, environment: str, payload: JSON) -> bool:
    allowed = normalize_scopes(scopes)
    if "*" in allowed or environment in allowed:
        return True
    checks = {
        "cluster": str(payload.get("cluster") or payload.get("cluster_id") or "").strip(),
        "namespace": str(payload.get("namespace") or "").strip(),
        "service": str(payload.get("service") or "").strip(),
        "team": str(payload.get("team") or "").strip(),
    }
    return any(f"{key}:{value}" in allowed for key, value in checks.items() if value)


def strip_secret_keys(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: JSON = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_MARKERS):
                continue
            redacted[key] = strip_secret_keys(item)
        return redacted
    if isinstance(value, list):
        return [strip_secret_keys(item) for item in value]
    return value


def _decode_row(row: JSON) -> JSON:
    if "settings_json" in row:
        row["settings"] = json.loads(row.pop("settings_json") or "{}")
    if "diff_json" in row:
        row["diff"] = json.loads(row.pop("diff_json") or "[]")
    if "critical_confirmed" in row:
        row["critical_confirmed"] = bool(int(row["critical_confirmed"] or 0))
    if "reload_required" in row:
        row["reload_required"] = bool(int(row["reload_required"] or 0))
    return row


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_DB = SettingsDB()


def current() -> JSON:
    return _DB.current()


def preview(payload: JSON) -> JSON:
    return _DB.preview(payload)


def save(payload: JSON, *, actor_id: str) -> JSON:
    return _DB.save(payload, actor_id=actor_id)


def rollback(*, actor_id: str) -> JSON:
    return _DB.rollback(actor_id=actor_id)


def policy_state() -> JSON:
    return _DB.policy_state()


def test_policy(payload: JSON, *, actor_id: str) -> JSON:
    return _DB.test_policy(payload, actor_id=actor_id)
