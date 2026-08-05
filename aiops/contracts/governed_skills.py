"""Strict cross-process contract for Gateway-authorized Skill bindings."""

from __future__ import annotations


JSON = dict[str, object]
_REFERENCE_FIELDS = {"integration_id", "integration_revision", "name", "version"}
_SKILL_FIELDS = {"id", "name", "version", "instruction", "workflow", "required_mcp"}
_VERSION_FIELDS = {"id", "name", "version"}


def normalize_skill_bindings(value: object) -> list[JSON]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("Skill bindings must be a bounded list")
    normalized: list[JSON] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _SKILL_FIELDS:
            raise ValueError("Skill binding fields are invalid")
        skill_id = _text(item["id"], "Skill id", 500)
        version = item["version"]
        if skill_id in ids or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Skill binding identity is invalid")
        ids.add(skill_id)
        instruction = _text(item["instruction"], "Skill instruction", 8_000, allow_empty=True)
        workflow = _workflow(item["workflow"])
        if not instruction and not workflow:
            raise ValueError("Skill binding content is empty")
        normalized.append({
            "id": skill_id,
            "name": _text(item["name"], "Skill name", 200),
            "version": version,
            "instruction": instruction,
            "workflow": workflow,
            "required_mcp": _references(item["required_mcp"]),
        })
    return normalized


def skill_versions(value: object) -> list[JSON]:
    return normalize_skill_versions([
        {"id": item["id"], "name": item["name"], "version": item["version"]}
        for item in normalize_skill_bindings(value)
    ])


def normalize_skill_versions(value: object) -> list[JSON]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("Skill versions must be a bounded list")
    normalized: list[JSON] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _VERSION_FIELDS:
            raise ValueError("Skill version fields are invalid")
        skill_id = _text(item["id"], "Skill id", 500)
        version = item["version"]
        if skill_id in ids or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Skill version identity is invalid")
        ids.add(skill_id)
        normalized.append({
            "id": skill_id,
            "name": _text(item["name"], "Skill name", 200),
            "version": version,
        })
    return normalized


def _workflow(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Skill workflow must be a bounded list")
    return [_text(item, "Skill workflow step", 1_000) for item in value]


def _references(value: object) -> list[JSON]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Required MCP references must be a bounded list")
    normalized: list[JSON] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _REFERENCE_FIELDS:
            raise ValueError("Required MCP reference fields are invalid")
        reference = {
            field: _text(item[field], f"Required MCP {field}", 500 if field.startswith("integration_") else 200)
            for field in _REFERENCE_FIELDS
        }
        key = (str(reference["integration_id"]), str(reference["name"]))
        if key in seen:
            raise ValueError("Required MCP references must be unique")
        seen.add(key)
        normalized.append(reference)
    return normalized


def _text(value: object, field: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"{field} is invalid")
    return normalized
