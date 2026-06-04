"""Shared time range contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeRange:
    """Relative or absolute time range used by V1 MCP tools."""

    type: str
    value: str
