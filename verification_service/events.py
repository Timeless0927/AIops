"""Shared structured stdout event contract for the verification fixture."""

from __future__ import annotations

import json


def emit_event(event: str, run_id: str) -> None:
    print(
        json.dumps({"event": event, "run_id": run_id}, separators=(",", ":"), sort_keys=True),
        flush=True,
    )
