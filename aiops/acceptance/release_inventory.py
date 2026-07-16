"""Canonical extracted-release inventory shared by P01 and Recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .redaction import redact_json


def build_release_inventory(root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": str(path.relative_to(root)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def inventory_artifact_sha256(inventory: list[dict[str, object]]) -> str:
    safe = redact_json(inventory)
    encoded = (
        json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
