"""Parse one terminal verification Job into its immutable run identity."""

from __future__ import annotations

import json
import math
from datetime import datetime

from .run_one_decisions import valid_run_id


def parse_verification_run(
    job_stdout: str, log_stdout: str,
) -> tuple[dict[str, object], dict[str, object], str, float]:
    job = json.loads(job_stdout)
    if not isinstance(job, dict):
        raise ValueError("verification trigger Job projection is invalid")
    metadata = job.get("metadata")
    status = job.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise ValueError("verification trigger Job omitted public identity or status")
    run_id = str(metadata.get("uid") or "")
    if (
        metadata.get("name") != "verification-trigger"
        or metadata.get("namespace") != "aiops-verification"
        or not valid_run_id(run_id)
        or status.get("succeeded") != 1
        or int(status.get("failed") or 0) != 0
    ):
        raise ValueError("verification trigger Job did not expose one successful run ID")
    trigger_started_at = _product_timestamp(status.get("startTime"))
    events = [json.loads(line) for line in log_stdout.splitlines() if line.strip()]
    trigger = {"event": "verification_trigger_job_succeeded", "run_id": run_id}
    if events.count(trigger) != 1:
        raise ValueError("verification trigger output does not match one Job controller UID")
    return job, trigger, run_id, trigger_started_at


def _product_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("verification Job startTime lacks timezone")
        timestamp = parsed.timestamp()
    else:
        raise ValueError("verification Job startTime is missing")
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("verification Job startTime is invalid")
    return timestamp
