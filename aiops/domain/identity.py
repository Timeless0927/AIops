"""Identity, LDAP boundary, and RBAC policy for Gateway/control-plane."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - JSON config still works in slim images
    yaml = None  # type: ignore[assignment]


ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_ONCALL_APPROVER = "oncall_approver"

PERMISSION_VIEW_INCIDENT = "view_incident"
PERMISSION_VIEW_COST = "view_cost"
PERMISSION_APPROVE_ACTION = "approve_action"
PERMISSION_CONFIGURE_NOTIFICATIONS = "configure_notifications"
PERMISSION_MANAGE_SERVICE_OWNERSHIP = "manage_service_ownership"
PERMISSION_EXECUTE_MUTATION = "execute_mutation"
PERMISSION_K8S_READ = "k8s_read"
PERMISSION_QUERY_AUDIT = "query_audit"
PERMISSION_SYNC_LDAP = "sync_ldap"

ROLE_ALIASES = {
    "administrator": ROLE_ADMIN,
    "normal_user": ROLE_USER,
    "ordinary_user": ROLE_USER,
    "approver": ROLE_ONCALL_APPROVER,
    "oncall": ROLE_ONCALL_APPROVER,
    "oncall_approver": ROLE_ONCALL_APPROVER,
    "duty_approver": ROLE_ONCALL_APPROVER,
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(
        {
            PERMISSION_VIEW_INCIDENT,
            PERMISSION_VIEW_COST,
            PERMISSION_APPROVE_ACTION,
            PERMISSION_CONFIGURE_NOTIFICATIONS,
            PERMISSION_MANAGE_SERVICE_OWNERSHIP,
            PERMISSION_EXECUTE_MUTATION,
            PERMISSION_K8S_READ,
            PERMISSION_QUERY_AUDIT,
            PERMISSION_SYNC_LDAP,
        }
    ),
    ROLE_USER: frozenset({PERMISSION_VIEW_INCIDENT, PERMISSION_VIEW_COST}),
    ROLE_ONCALL_APPROVER: frozenset(
        {
            PERMISSION_VIEW_INCIDENT,
            PERMISSION_VIEW_COST,
            PERMISSION_APPROVE_ACTION,
            PERMISSION_K8S_READ,
        }
    ),
}


class IdentityError(ValueError):
    """Controlled identity/authentication failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Scope:
    """Resource scope for service/team/namespace authorization."""

    services: tuple[str, ...] = ("*",)
    teams: tuple[str, ...] = ("*",)
    namespaces: tuple[str, ...] = ("*",)

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "Scope":
        value = value or {}
        return cls(
            services=_normalize_scope_values(value.get("services"), default=("*",)),
            teams=_normalize_scope_values(value.get("teams"), default=("*",)),
            namespaces=_normalize_scope_values(value.get("namespaces"), default=("*",)),
        )

    def matches(self, resource: "Scope") -> bool:
        return (
            _scope_dimension_allows(self.services, resource.services)
            and _scope_dimension_allows(self.teams, resource.teams)
            and _scope_dimension_allows(self.namespaces, resource.namespaces)
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "services": list(self.services),
            "teams": list(self.teams),
            "namespaces": list(self.namespaces),
        }


@dataclass(frozen=True)
class Actor:
    """Authenticated user identity consumed by Gateway and audit records."""

    actor_id: str
    username: str
    display_name: str
    email: str | None = None
    roles: tuple[str, ...] = (ROLE_USER,)
    scope: Scope = field(default_factory=Scope)
    groups: tuple[str, ...] = ()
    department: str | None = None
    auth_source: str = "ldap"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Actor":
        username = str(value.get("username") or value.get("uid") or "").strip()
        if not username:
            raise IdentityError("invalid_actor", "username is required")
        roles = tuple(_normalize_role(role) for role in _as_string_tuple(value.get("roles") or value.get("role") or [ROLE_USER]))
        actor_id = str(value.get("actor_id") or value.get("id") or username).strip()
        return cls(
            actor_id=actor_id,
            username=username,
            display_name=str(value.get("display_name") or value.get("name") or username).strip(),
            email=_optional_str(value.get("email")),
            roles=roles or (ROLE_USER,),
            scope=Scope.from_mapping(value.get("scope") if isinstance(value.get("scope"), dict) else None),
            groups=_as_string_tuple(value.get("groups")),
            department=_optional_str(value.get("department") or value.get("team")),
            auth_source=str(value.get("auth_source") or "ldap").strip() or "ldap",
        )

    def has_role(self, role: str) -> bool:
        return _normalize_role(role) in self.roles

    def permissions(self) -> frozenset[str]:
        values: set[str] = set()
        for role in self.roles:
            values.update(ROLE_PERMISSIONS.get(role, frozenset()))
        return frozenset(values)

    def can(self, permission: str, resource_scope: Scope | None = None) -> bool:
        if permission not in self.permissions():
            return False
        if self.has_role(ROLE_ADMIN):
            return True
        return self.scope.matches(resource_scope or Scope())

    def audit_context(self, request_id: str) -> dict[str, Any]:
        return {
            "actor": self.actor_id,
            "username": self.username,
            "roles": list(self.roles),
            "scope": self.scope.to_dict(),
            "request_id": request_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "roles": list(self.roles),
            "scope": self.scope.to_dict(),
            "groups": list(self.groups),
            "department": self.department,
            "auth_source": self.auth_source,
            "permissions": sorted(self.permissions()),
        }


@dataclass(frozen=True)
class LdapSettings:
    """LDAP connection settings loaded from env or Gateway identity config."""

    enabled: bool = False
    url: str = ""
    bind_dn: str = ""
    bind_password: str = ""
    user_base_dn: str = ""
    user_filter: str = "(uid={username})"
    group_base_dn: str = ""
    group_filter: str = "(member={user_dn})"
    use_ssl: bool = True
    ca_file: str | None = None
    sync_enabled: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "LdapSettings":
        value = value or {}
        return cls(
            enabled=_as_bool(value.get("enabled"), default=False),
            url=str(value.get("url") or os.getenv("AIOPS_LDAP_URL") or "").strip(),
            bind_dn=str(value.get("bind_dn") or os.getenv("AIOPS_LDAP_BIND_DN") or "").strip(),
            bind_password=str(value.get("bind_password") or os.getenv("AIOPS_LDAP_BIND_PASSWORD") or "").strip(),
            user_base_dn=str(value.get("user_base_dn") or os.getenv("AIOPS_LDAP_USER_BASE_DN") or "").strip(),
            user_filter=str(value.get("user_filter") or os.getenv("AIOPS_LDAP_USER_FILTER") or "(uid={username})").strip(),
            group_base_dn=str(value.get("group_base_dn") or os.getenv("AIOPS_LDAP_GROUP_BASE_DN") or "").strip(),
            group_filter=str(value.get("group_filter") or os.getenv("AIOPS_LDAP_GROUP_FILTER") or "(member={user_dn})").strip(),
            use_ssl=_as_bool(value.get("use_ssl"), default=True),
            ca_file=_optional_str(value.get("ca_file") or os.getenv("AIOPS_LDAP_CA_FILE")),
            sync_enabled=_as_bool(value.get("sync_enabled"), default=False),
        )

    def validate_for_login(self) -> None:
        missing = [
            name
            for name, raw in (
                ("url", self.url),
                ("bind_dn", self.bind_dn),
                ("bind_password", self.bind_password),
                ("user_base_dn", self.user_base_dn),
            )
            if not raw
        ]
        if missing:
            raise IdentityError("ldap_config_incomplete", f"missing LDAP settings: {', '.join(missing)}")


@dataclass(frozen=True)
class IdentityConfig:
    """Gateway identity config loaded from a YAML/JSON file or env."""

    ldap: LdapSettings = field(default_factory=LdapSettings)
    static_users: tuple[dict[str, Any], ...] = ()
    group_role_map: dict[str, str] = field(default_factory=dict)
    group_scope_map: dict[str, Scope] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "IdentityConfig":
        config_path = path or os.getenv("AIOPS_IDENTITY_CONFIG")
        raw: dict[str, Any] = {}
        if config_path:
            raw = _read_config(Path(config_path).expanduser())
        identity = raw.get("identity") if isinstance(raw.get("identity"), dict) else raw
        if not isinstance(identity, dict):
            identity = {}
        ldap = LdapSettings.from_mapping(identity.get("ldap") if isinstance(identity.get("ldap"), dict) else None)
        static_users = identity.get("users") or identity.get("static_users") or ()
        if not isinstance(static_users, list):
            static_users = []
        role_map = identity.get("group_role_map") if isinstance(identity.get("group_role_map"), dict) else {}
        scope_map_raw = identity.get("group_scope_map") if isinstance(identity.get("group_scope_map"), dict) else {}
        scope_map = {str(group): Scope.from_mapping(scope if isinstance(scope, dict) else {}) for group, scope in scope_map_raw.items()}
        return cls(
            ldap=ldap,
            static_users=tuple(item for item in static_users if isinstance(item, dict)),
            group_role_map={str(group): _normalize_role(role) for group, role in role_map.items()},
            group_scope_map=scope_map,
        )


@dataclass(frozen=True)
class AuthSession:
    token: str
    actor: Actor
    created_at: float
    expires_at: float


class SessionTokenStore:
    """In-memory bearer token store for the lightweight Gateway HTTP surface."""

    def __init__(self, ttl_seconds: int = 8 * 60 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, AuthSession] = {}

    def issue(self, actor: Actor) -> AuthSession:
        now = time.time()
        token = secrets.token_urlsafe(32)
        session = AuthSession(token=token, actor=actor, created_at=now, expires_at=now + self.ttl_seconds)
        self._sessions[token] = session
        return session

    def get(self, token: str) -> AuthSession | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            return None
        return session

    def clear(self) -> None:
        self._sessions.clear()


class IdentityProvider:
    """LDAP-backed identity provider with static fixture support for tests/dev."""

    def __init__(self, config: IdentityConfig | None = None) -> None:
        self.config = config or IdentityConfig.load()

    def login(self, username: str, password: str) -> Actor:
        username = username.strip()
        if not username or not password:
            raise IdentityError("invalid_credentials", "username and password are required")
        actor = self._login_static_user(username, password)
        if actor is not None:
            return actor
        if not self.config.ldap.enabled:
            raise IdentityError("ldap_not_configured", "LDAP login is not configured")
        return self._login_ldap(username, password)

    def sync_users(self) -> tuple[Actor, ...]:
        if self.config.static_users:
            return tuple(Actor.from_mapping({**user, "auth_source": user.get("auth_source") or "ldap_fixture"}) for user in self.config.static_users)
        if not self.config.ldap.enabled:
            raise IdentityError("ldap_not_configured", "LDAP sync is not configured")
        self.config.ldap.validate_for_login()
        if importlib.util.find_spec("ldap3") is None:
            raise IdentityError("ldap_dependency_missing", "ldap3 is required for LDAP sync")
        raise IdentityError("ldap_sync_unimplemented", "LDAP sync adapter is configured but not connected in this runtime")

    def _login_static_user(self, username: str, password: str) -> Actor | None:
        for user in self.config.static_users:
            if str(user.get("username") or "").strip() != username:
                continue
            expected = str(user.get("password") or "").strip()
            if not expected or not secrets.compare_digest(expected, password):
                raise IdentityError("invalid_credentials", "invalid username or password")
            return Actor.from_mapping({**user, "auth_source": user.get("auth_source") or "ldap_fixture"})
        return None

    def _login_ldap(self, username: str, password: str) -> Actor:
        settings = self.config.ldap
        settings.validate_for_login()
        if importlib.util.find_spec("ldap3") is None:
            raise IdentityError("ldap_dependency_missing", "ldap3 is required for LDAP login")

        import ldap3  # type: ignore[import-not-found]

        server = ldap3.Server(settings.url, use_ssl=settings.use_ssl, tls=None)
        service_conn = ldap3.Connection(server, user=settings.bind_dn, password=settings.bind_password, auto_bind=True)
        user_filter = settings.user_filter.format(username=_escape_ldap_filter(username))
        service_conn.search(settings.user_base_dn, user_filter, attributes=["cn", "mail", "memberOf", "department"])
        if not service_conn.entries:
            raise IdentityError("invalid_credentials", "invalid username or password")
        entry = service_conn.entries[0]
        user_dn = str(entry.entry_dn)
        user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=False)
        if not user_conn.bind():
            raise IdentityError("invalid_credentials", "invalid username or password")
        groups = tuple(str(group) for group in getattr(entry, "memberOf", []) or ())
        roles = self._roles_for_groups(groups)
        scope = self._scope_for_groups(groups)
        return Actor(
            actor_id=user_dn,
            username=username,
            display_name=str(getattr(entry, "cn", username) or username),
            email=str(getattr(entry, "mail", "") or "") or None,
            roles=roles or (ROLE_USER,),
            scope=scope,
            groups=groups,
            department=str(getattr(entry, "department", "") or "") or None,
            auth_source="ldap",
        )

    def _roles_for_groups(self, groups: tuple[str, ...]) -> tuple[str, ...]:
        roles = {_normalize_role(self.config.group_role_map[group]) for group in groups if group in self.config.group_role_map}
        return tuple(sorted(roles)) or (ROLE_USER,)

    def _scope_for_groups(self, groups: tuple[str, ...]) -> Scope:
        services: set[str] = set()
        teams: set[str] = set()
        namespaces: set[str] = set()
        for group in groups:
            scope = self.config.group_scope_map.get(group)
            if scope is None:
                continue
            services.update(scope.services)
            teams.update(scope.teams)
            namespaces.update(scope.namespaces)
        if not services and not teams and not namespaces:
            return Scope()
        return Scope(
            services=tuple(sorted(services or {"*"})),
            teams=tuple(sorted(teams or {"*"})),
            namespaces=tuple(sorted(namespaces or {"*"})),
        )


def role_permission_matrix() -> dict[str, list[str]]:
    """Return a stable role/permission matrix for API responses and docs."""
    return {role: sorted(permissions) for role, permissions in sorted(ROLE_PERMISSIONS.items())}


def resource_scope(
    *,
    service: str | None = None,
    team: str | None = None,
    namespace: str | None = None,
) -> Scope:
    return Scope(
        services=(service.strip(),) if service and service.strip() else ("*",),
        teams=(team.strip(),) if team and team.strip() else ("*",),
        namespaces=(namespace.strip(),) if namespace and namespace.strip() else ("*",),
    )


def is_allowed(actor: Actor, permission: str, scope: Scope | None = None) -> bool:
    return actor.can(permission, scope)


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise IdentityError("identity_config_missing", f"identity config not found: {path}")
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(content or "{}")
    elif yaml is not None:
        data = yaml.safe_load(content) or {}
    else:
        data = json.loads(content or "{}")
    if not isinstance(data, dict):
        raise IdentityError("identity_config_invalid", "identity config must be an object")
    return data


def _normalize_role(role: Any) -> str:
    value = str(role or ROLE_USER).strip().lower().replace("-", "_")
    return ROLE_ALIASES.get(value, value)


def _normalize_scope_values(value: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    values = _as_string_tuple(value)
    return values or default


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _scope_dimension_allows(allowed: tuple[str, ...], requested: tuple[str, ...]) -> bool:
    if "*" in allowed:
        return True
    if "*" in requested:
        return False
    return any(value in allowed for value in requested)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _escape_ldap_filter(value: str) -> str:
    return (
        value.replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )
