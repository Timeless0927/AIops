from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aiops.acceptance.release_inventory import (
    build_release_inventory,
    inventory_artifact_sha256,
)


def test_release_inventory_hash_is_exact_evidence_json_artifact_sha(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "manifest.yaml").write_text("kind: List\n")
    (release / "nested").mkdir()
    (release / "nested" / "release.json").write_text('{"version":"v1"}\n')

    inventory = build_release_inventory(release)
    encoded = (
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()

    assert inventory_artifact_sha256(inventory) == hashlib.sha256(encoded).hexdigest()
    assert [item["path"] for item in inventory] == [
        "manifest.yaml", "nested/release.json",
    ]
