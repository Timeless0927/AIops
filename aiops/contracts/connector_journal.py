"""Connector terminal journal evidence shared across the process boundary."""

from __future__ import annotations

import hashlib
import json


def terminal_journal_evidence(
    command_id: str, result: dict[str, object], *, recorded_at: float,
) -> dict[str, object]:
    encoded = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return {
        "state": "terminal",
        "command_id": command_id,
        "result_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "recorded_at": recorded_at,
    }
