"""Structured read-tool audit without module-level persistence."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any


logger = logging.getLogger(__name__)


async def record_audit(**fields: Any) -> str:
    audit_id = f"audit-{uuid.uuid4().hex}"
    logger.info(
        json.dumps(
            {"event": "tool_audit", "audit_id": audit_id, **fields},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )
    return audit_id
