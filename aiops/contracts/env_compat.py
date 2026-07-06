"""Runtime env compatibility helpers."""

from __future__ import annotations

import os


def compat_env(name: str, legacy_name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    value = os.getenv(legacy_name, "").strip()
    return value if value else default


def compat_float_env(name: str, legacy_name: str, default: float) -> float:
    try:
        return max(0.1, float(compat_env(name, legacy_name, str(default))))
    except ValueError:
        return default
